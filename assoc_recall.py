"""Internal associative recall protocol, evidence checks, and settlement ledger.

The searchable cue index lives in ``assoc_index``. This module owns the wire
contract and the independent, idempotent ledger for selections made from it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from typing import Any

SCHEMA_VERSION = "assoc.v1"
MAX_CANDIDATES = 6
TOKEN_TTL_SECONDS = 900


def _scope(body: dict) -> dict[str, Any] | None:
    value = body.get("scope")
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {key: str(value.get(key) or "").strip() for key in (
        "session_id", "branch_id", "user_message_id", "message_version", "request_id"
    )}
    if not all(result.values()):
        return None
    # Presence is meaningful: [] says the active path has no completed prior
    # assistant turns, while absence retains the legacy TTL-based behavior.
    if "last_assistant_ids" in value:
        ids = value["last_assistant_ids"]
        if not isinstance(ids, (list, tuple)):
            return None
        result["last_assistant_ids"] = list(dict.fromkeys(
            str(item).strip() for item in ids
            if isinstance(item, (str, int)) and not isinstance(item, bool) and str(item).strip()
        ))[-6:]
    return result


def _valid_body(body: Any, *, needs_scope: bool = True) -> bool:
    return (isinstance(body, dict)
            and body.get("schema_version") == SCHEMA_VERSION
            and (not needs_scope or _scope(body) is not None))


def _json_object(text: str) -> dict | None:
    """Parse a complete JSON object, tolerating a single fenced response."""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


class AssociativeRecall:
    """Bounded associative recall service backed by ``AssocIndex``."""

    def __init__(self, config: dict, index, dehydrator, embedding_engine):
        self.index = index
        self.dehydrator = dehydrator
        self.embedding = embedding_engine
        state_dir = config.get("state_dir") or os.path.join(
            os.path.dirname(config["buckets_dir"]), "state"
        )
        os.makedirs(state_dir, exist_ok=True)
        self.db_path = os.path.join(state_dir, "assoc_recall.sqlite3")
        self._worker: asyncio.Task | None = None
        self._worker_lock = asyncio.Lock()
        self._init_db()

    def _db(self, *, timeout=5):
        db = sqlite3.connect(self.db_path, timeout=timeout)
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self) -> None:
        with closing(self._db()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS selections (
                    selection_token TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    branch_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    evidence_version TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    outcome TEXT,
                    assistant_message_id TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS assoc_finish_once
                    ON selections(session_id, branch_id, assistant_message_id, event_id)
                    WHERE assistant_message_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS assoc_selection_expiry
                    ON selections(expires_at);
                CREATE INDEX IF NOT EXISTS assoc_session_cooldown
                    ON selections(session_id, event_id, outcome, expires_at);
            """)

    async def _ensure_worker(self) -> None:
        """Run small incremental refresh batches, never on the request's critical path."""
        async with self._worker_lock:
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        # Keep the index current even when no-one opens a conversation. Raw
        # source revisions are still checked again before any candidate is used.
        if hasattr(self.index,"warm"):
            self.index.warm()
        await asyncio.sleep(15)
        while True:
            try:
                report=await self.index.rebuild(limit=12)
                from assoc_plans import refresh_plan_states
                plans=await refresh_plan_states(self.index,limit=12)
                if plans.get("remaining") and not plans.get("failed"):
                    report["complete"]=False
                delay=300 if report.get("complete") else 20
            except asyncio.CancelledError:
                raise
            except Exception:
                delay=90
            await asyncio.sleep(delay)

    async def _ask(self, system: str, user: str, *, max_tokens: int = 320) -> str:
        """Use the configured dehydration provider/model; no new client or dependency."""
        if not getattr(self.dehydrator, "api_available", False):
            return ""
        from assoc_budget import reserve
        await asyncio.to_thread(reserve,self.db_path,ceiling_usd=8.5,text=system+user[:9000],output=max_tokens,retries=3)
        response = await self.dehydrator.client.with_options(max_retries=0).chat.completions.create(
            model=self.dehydrator.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user[:9000]}],
            **self.dehydrator._completion_options(
                max_tokens=max_tokens, temperature=0.0
            ),
        )
        if not response.choices:
            return ""
        return str(response.choices[0].message.content or "")

    async def _predict(self, context: str) -> list[str]:
        raw = await self._ask(
            "From this recent conversation, return JSON only: "
            '{"situations":[...],"possible_questions":[...]}. '
            "Return at most one concise situation cue and one plausible follow-up question, "
            "when each adds a distinct search direction. Keep them complementary and concise. "
            "Do not invent past facts; these are search queries only.",
            context,
            max_tokens=240,
        )
        parsed = _json_object(raw) or {}
        # Prioritize one situation and one question for semantic search; lexical
        # recall can still use other distinct formulations the predictor returns.
        groups=[]
        for key in ("situations","possible_questions"):
            items=parsed.get(key)
            group=[x.strip()[:400] for x in items if isinstance(x,str) and x.strip()] if isinstance(items,list) else []
            groups.append(group)
        ordered=[g[0] for g in groups if g]+[v for g in groups for v in g[1:]]
        result=[];seen=set()
        for value in ordered:
            key="".join(value.lower().split()).strip("。！？?!")
            if key and key not in seen:
                seen.add(key);result.append(value)
        return result[:4]

    async def _judge(self, context: str, candidates: list[dict], *, query_mode: bool) -> list[dict]:
        if not candidates:
            return []
        allowed = [{"id": str(i), "excerpt": str(c.get("evidence_context") or c.get("excerpt") or "")[:300],
                    "reason": str(c.get("reason") or "")[:240],
                    "date": str(c.get("date") or ""),
                    "plan_state": str(c.get("plan_state") or "unknown"),
                    "plan_quote": str(c.get("plan_quote") or "")[:220],
                    "outcome_state": str(c.get("outcome_state") or "unknown"),
                    "outcome_quote": str(c.get("outcome_quote") or "")[:220],
                    "archived": bool(c.get("archived"))}
                   for i, c in enumerate(candidates)]
        if query_mode:
            instruction = (
                "Compare the current user turn with each candidate side by side. Return JSON only: "
                "{\"selections\":[{\"id\":\"0\",\"use\":\"mention|understand\",\"reason\":\"...\"}]}. "
                "Choose zero or one. Select only when the evidence directly helps this turn. "
                "Return zero for resting, venting, an unfinished thought, or topic changes. "
                "Do not infer motives, outcomes, or events from dates. Archived memories need a clear "
                "matching situation/action or a distinctive detail plus context."
            )
        else:
            instruction = (
                "Compare each candidate with this recent conversation to prepare possible future recall. "
                "Return JSON only: {\"selections\":[{\"id\":\"0\",\"reason\":\"shared situation/action/outcome\"}]}. "
                "Choose zero to six candidates, each supported by its excerpt. Do not decide whether to "
                "mention a memory now; this is only a shortlist for a later user turn. Do not infer motives, "
                "outcomes, or events from dates. Prefer specific comparable situations and distinctive details. "
                "Use short numeric ids exactly as provided."
            )
        raw = await self._ask(instruction, json.dumps({"context": context[-4500:],
                                                       "candidates": allowed}, ensure_ascii=False),
                              max_tokens=240 if query_mode else 500)
        parsed = _json_object(raw)
        choices = parsed.get("selections") if parsed else None
        if not isinstance(choices, list):
            return []
        by_id = {str(i): c for i, c in enumerate(candidates)}
        output = []
        for choice in choices[:1 if query_mode else MAX_CANDIDATES]:
            if not isinstance(choice, dict):
                continue
            candidate = by_id.get(str(choice.get("id") or ""))
            if not candidate:
                continue
            item = {**candidate, "reason": str(choice.get("reason") or candidate.get("reason") or "")[:240]}
            if query_mode:
                use = str(choice.get("use") or "")
                if use not in {"mention", "understand"}:
                    continue
                item["use"] = use
            output.append(item)
        return output

    async def prepare(self, body: dict) -> dict:
        empty = {"schema_version": SCHEMA_VERSION, "status": "ok", "candidates": []}
        if not _valid_body(body):
            return {"schema_version": SCHEMA_VERSION, "status": "invalid"}
        await self._ensure_worker()
        context = str(body.get("context_text") or "").strip()[:8000]
        if not context:
            return empty
        try:
            searches = await self._predict(context)
            if not searches:
                searches = [context[-1200:]]
            rows = await self.index.search(searches, limit=24, allow_embed=True)
            # Interleave the different prediction queries by event, so one broad
            # search phrase cannot crowd out all other cues.
            candidates, seen = [], set()
            for row in rows:
                candidate_id = str(row.get("candidate_id") or "")
                event_id = str(row.get("event_id") or "")
                if not candidate_id or not event_id or event_id in seen:
                    continue
                seen.add(event_id)
                candidates.append({
                    "candidate_id": candidate_id,
                    "event_id": event_id,
                    "evidence_version": str(row.get("evidence_version") or ""),
                    "excerpt": str(row.get("excerpt") or "")[:300],
                    "evidence_context": str(row.get("source_window") or "")[:260],
                    "reason": str(row.get("reason") or "matching situation or detail")[:240],
                    "date": str(row.get("date") or ""),
                    "plan_state": str(row.get("plan_state") or "unknown"),
                    "plan_quote": str(row.get("plan_quote") or "")[:220],
                    "outcome_state": str(row.get("outcome_state") or "unknown"),
                    "outcome_quote": str(row.get("outcome_quote") or "")[:220],
                    "outcome_source": str(row.get("outcome_source") or "")[:280],
                    "source_revision": str(row.get("source_revision") or "")[:80],
                    "archived": bool(row.get("archived")),
                    "score": float(row.get("score") or 0),
                })
            judged = await self._judge(context, candidates[:24], query_mode=False)
            output = [{key: item.get(key) for key in (
                "candidate_id", "event_id", "evidence_version", "excerpt", "evidence_context", "reason", "date", "archived",
                "plan_state", "plan_quote", "outcome_state", "outcome_quote", "source_ref", "source_revision"
            )} for item in judged][:MAX_CANDIDATES]
            return {"schema_version": SCHEMA_VERSION, "status": "ok", "candidates": output}
        except Exception:
            return empty

    async def _resolve(self, candidate_id: str, version: str, quote: str):
        if not candidate_id or not version:
            return None
        try:
            row = await self.index.resolve(candidate_id, expected_revision=version, quote=quote)
        except Exception:
            return None
        if not row or str(row.get("evidence_version") or "") != version:
            return None
        window = str(row.get("source_window") or "")
        if not window:
            return None
        return row

    def _is_cooled(self, scope: dict[str, Any], event_id: str) -> bool:
        session_id = str(scope["session_id"])
        if "last_assistant_ids" in scope:
            assistant_ids = scope["last_assistant_ids"]
            if not assistant_ids:
                return False
            placeholders = ",".join("?" for _ in assistant_ids)
            with closing(self._db()) as db:
                row = db.execute(
                    "SELECT 1 FROM selections WHERE session_id=? AND event_id=? "
                    "AND outcome='mentioned' AND assistant_message_id IN (" + placeholders + ") LIMIT 1",
                    (session_id, event_id, *assistant_ids),
                ).fetchone()
            return row is not None
        # Older gateways do not send active-path turn IDs. Keep their existing
        # receipt-expiry fallback until they are upgraded.
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT 1 FROM selections WHERE session_id=? AND event_id=? "
                "AND outcome='mentioned' AND expires_at>? ORDER BY rowid DESC LIMIT 6",
                (session_id, event_id, time.time()),
            ).fetchall()
        return bool(rows)

    def _reserve(self, scope: dict[str, str], candidate: dict) -> str:
        token = secrets.token_urlsafe(24)
        with closing(self._db(timeout=0)) as db, db:
            db.execute("DELETE FROM selections WHERE expires_at<? AND outcome IS NULL", (time.time(),))
            db.execute(
                "INSERT INTO selections(selection_token,session_id,branch_id,request_id,event_id,"
                "candidate_id,evidence_version,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                (token, scope["session_id"], scope["branch_id"], scope["request_id"],
                 candidate["event_id"], candidate["candidate_id"],
                 candidate["evidence_version"], time.time() + TOKEN_TTL_SECONDS),
            )
        return token

    async def validate(self, body: dict) -> dict:
        if not _valid_body(body):
            return {"schema_version": SCHEMA_VERSION, "status": "invalid", "candidates": []}
        scope = _scope(body)
        recheck = body.get("recheck_only") is True
        output = []
        raw = body.get("candidates")
        if not isinstance(raw, list):
            raw = []
        for candidate in raw[:MAX_CANDIDATES]:
            if not isinstance(candidate, dict):
                continue
            candidate_id = str(candidate.get("candidate_id") or "").strip()
            version = str(candidate.get("evidence_version") or "").strip()
            if recheck and not all(isinstance(candidate.get(key), str) and candidate[key].strip()
                                   for key in ("selection_token", "event_id", "excerpt")):
                continue
            row = await self._resolve(candidate_id, version, str(candidate.get("excerpt") or ""))
            if not row or self._is_cooled(scope, str(row.get("event_id") or "")):
                continue
            if recheck:
                token = candidate["selection_token"]
                if row.get("excerpt") != candidate["excerpt"] or row.get("event_id") != candidate["event_id"]:
                    continue
                with closing(self._db(timeout=0)) as db:
                    reserved = db.execute(
                        "SELECT 1 FROM selections WHERE selection_token=? AND session_id=? AND branch_id=? "
                        "AND request_id=? AND candidate_id=? AND event_id=? AND evidence_version=? "
                        "AND outcome IS NULL AND expires_at>?",
                        (token, scope["session_id"], scope["branch_id"], scope["request_id"],
                         candidate_id, row["event_id"], version, time.time()),
                    ).fetchone()
                if not reserved:
                    continue
            else:
                try:
                    token = self._reserve(scope, {
                        "candidate_id": candidate_id,
                        "event_id": str(row["event_id"]),
                        "evidence_version": version,
                    })
                except (sqlite3.IntegrityError, sqlite3.OperationalError):
                    continue
            output.append({
                "candidate_id": candidate_id,
                "event_id": str(row["event_id"]),
                "evidence_version": version,
                "excerpt": str(row.get("excerpt") or candidate.get("excerpt") or "")[:300],
                "reason": str(candidate.get("reason") or "")[:240],
                "date": str(row.get("date") or candidate.get("date") or ""),
                "archived": bool(row.get("archived", candidate.get("archived", False))),
                "plan_state": str(row.get("plan_state") or "unknown"),
                "plan_quote": str(row.get("plan_quote") or "")[:220],
                "outcome_state": str(row.get("outcome_state") or "unknown"),
                "outcome_quote": str(row.get("outcome_quote") or "")[:220],
                "outcome_source": str(row.get("outcome_source") or "")[:280],
                "source_revision": str(row.get("source_revision") or version)[:80],
                "source_window": str(row["source_window"]),
                "selection_token": token,
            })
        return {"schema_version": SCHEMA_VERSION, "status": "ok", "candidates": output,
                **({"recheck_only": True} if recheck else {})}

    async def query(self, body: dict) -> dict:
        empty = {"schema_version": SCHEMA_VERSION, "status": "ok", "selected": None}
        if not _valid_body(body):
            return {"schema_version": SCHEMA_VERSION, "status": "invalid", "selected": None}
        scope = _scope(body)
        literal = body.get("query_text")
        if not isinstance(literal, str) or not literal.strip():
            return empty
        context = str(body.get("context_text") or "")[:5000]
        remaining = max(0, min(int(body.get("remaining_ms") or 0), 15000))
        try:
            if hasattr(self.index, "warm"): self.index.warm()
            # This only reads a vector already generated/cached by the literal
            # breath query. It never starts an embedding request.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + remaining / 1000
            # Give the literal recall task a brief scheduling opportunity to
            # register its shared embedding, without starting another request.
            if remaining > 0:
                await asyncio.sleep(min(0.005, remaining / 1000))
            left_ms = max(0, int((deadline - loop.time()) * 1000))
            vector = await self.embedding.wait_query_embedding(literal, timeout_ms=min(4000,left_ms))
            left = deadline - loop.time()
            if left <= 0:
                return empty
            rows = await asyncio.wait_for(
                self.index.search([literal], limit=5, vector=vector, allow_embed=False), timeout=left
            )
            candidates = []
            for row in rows:
                if not row.get("candidate_id") or not row.get("event_id") or not row.get("evidence_version"):
                    continue
                candidates.append({
                    "candidate_id": str(row["candidate_id"]),
                    "event_id": str(row["event_id"]),
                    "evidence_version": str(row["evidence_version"]),
                    "excerpt": str(row.get("excerpt") or "")[:300],
                    "evidence_context": str(row.get("source_window") or "")[:260],
                    "reason": str(row.get("reason") or "matching query detail")[:240],
                    "date": str(row.get("date") or ""),
                    "plan_state": str(row.get("plan_state") or "unknown"),
                    "plan_quote": str(row.get("plan_quote") or "")[:220],
                    "outcome_state": str(row.get("outcome_state") or "unknown"),
                    "outcome_quote": str(row.get("outcome_quote") or "")[:220],
                    "outcome_source": str(row.get("outcome_source") or "")[:280],
                    "source_revision": str(row.get("source_revision") or "")[:80],
                    "archived": bool(row.get("archived")),
                })
            left = deadline - loop.time()
            if left <= 0:
                return empty
            judged = await asyncio.wait_for(
                self._judge(context, candidates, query_mode=True), timeout=left
            )
            if not judged:
                return empty
            item = judged[0]
            if self._is_cooled(scope, item["event_id"]):
                return empty
            row = await self._resolve(item["candidate_id"], item["evidence_version"], str(item.get("excerpt") or ""))
            if not row or str(row.get("event_id") or "") != item["event_id"]:
                return empty
            token = self._reserve(scope, item)
            return {"schema_version": SCHEMA_VERSION, "status": "ok", "selected": {
                "candidate_id": item["candidate_id"],
                "event_id": item["event_id"],
                "evidence_version": item["evidence_version"],
                "excerpt": str(row.get("excerpt") or item.get("excerpt") or "")[:300],
                "reason": item.get("reason", ""),
                "date": str(row.get("date") or item.get("date") or ""),
                "archived": bool(row.get("archived", item.get("archived", False))),
                "source_window": str(row["source_window"]),
                "plan_state": str(row.get("plan_state") or "unknown"),
                "plan_quote": str(row.get("plan_quote") or "")[:220],
                "outcome_state": str(row.get("outcome_state") or "unknown"),
                "outcome_quote": str(row.get("outcome_quote") or "")[:220],
                "outcome_source": str(row.get("outcome_source") or "")[:280],
                "source_revision": str(row.get("source_revision") or "")[:80],
                "selection_token": token,
                "use": item["use"],
            }}
        except Exception:
            return empty

    def finish(self, body: dict) -> dict:
        if not _valid_body(body, needs_scope=False):
            return {"schema_version": SCHEMA_VERSION, "status": "invalid"}
        token = str(body.get("selection_token") or "").strip()
        assistant_id = str(body.get("assistant_message_id") or "").strip()
        outcome = str(body.get("outcome") or "")
        if not token or outcome not in {"mentioned", "unused", "cancelled"}:
            return {"schema_version": SCHEMA_VERSION, "status": "ignored"}
        scope = _scope(body)
        if scope is None: return {"schema_version":SCHEMA_VERSION,"status":"invalid"}
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM selections WHERE selection_token=? AND expires_at>?",
                             (token, time.time())).fetchone()
            if not row or (scope and (row["session_id"] != scope["session_id"] or
                                      row["branch_id"] != scope["branch_id"] or
                                      row["request_id"] != scope["request_id"])):
                db.rollback()
                return {"schema_version": SCHEMA_VERSION, "status": "ignored"}
            if row["outcome"] is not None:
                db.rollback()
                return {"schema_version": SCHEMA_VERSION, "status": "already_settled"}
            if outcome == "mentioned" and not assistant_id:
                db.rollback()
                return {"schema_version": SCHEMA_VERSION, "status": "ignored"}
            try:
                db.execute("UPDATE selections SET outcome=?, assistant_message_id=? WHERE selection_token=?",
                           (outcome, assistant_id or None, token))
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                return {"schema_version": SCHEMA_VERSION, "status": "already_settled"}
        return {"schema_version": SCHEMA_VERSION, "status": "ok"}
