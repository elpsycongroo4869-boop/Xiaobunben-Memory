"""Evidence-grounded associative recall index.

Generated cue text is used only for retrieval. Every returned result is tied to a
literal quote in a current moment and is revalidated against the live bucket.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
import asyncio
import threading
import numpy as np
from assoc_budget import reserve
from collections import Counter
from contextlib import closing
from index_lock import try_lock
from self_anchor import is_self_anchor_bucket
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "assoc.v1"
GENERATOR_VERSION = "assoc-cues-1"
_MAX_CUE_CHARS = 700
_MAX_QUOTE_CHARS = 220
_MAX_CUES_PER_MOMENT = 4
_MAX_TEXT_CHARS = 6000
_STOP = {
    "然后", "今天", "昨天", "明天", "一个", "这个", "那个", "我们", "你们",
    "他们", "她们", "就是", "因为", "所以", "但是", "如果", "怎么", "什么",
    "the", "and", "for", "are", "but", "not", "you", "with", "this", "that",
}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _metadata(moment: dict) -> dict:
    value = moment.get("metadata")
    if isinstance(value, dict):
        return value
    try:
        value = json.loads(moment.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _finite_vector(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    try:
        result = [float(x) for x in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in result):
        return None
    norm = math.sqrt(sum(x * x for x in result))
    return [x / norm for x in result] if norm else None


def _cosine(left: Any, right: Any) -> float | None:
    a, b = _finite_vector(left), _finite_vector(right)
    if not a or not b or len(a) != len(b):
        return None
    return sum(x * y for x, y in zip(a, b))


def _tokens(text: str) -> list[str]:
    # Keep Chinese runs and useful Latin words; no dependency on a tokenizer.
    words = re.findall(r"[a-z0-9_'-]{2,}|[\u3400-\u9fff]+", str(text or "").lower())
    return [term for w in words for term in
            ([w[i:i+2] for i in range(len(w)-1)] if re.search(r"[\u3400-\u9fff]", w) else [w])
            if term not in _STOP]



def _date(moment: dict, bucket: dict) -> str:
    meta = bucket.get("metadata") if isinstance(bucket.get("metadata"), dict) else {}
    mmeta = _metadata(moment)
    return str(meta.get("date") or meta.get("created") or mmeta.get("date") or moment.get("created_at") or "")[:40]


class AssocIndex:
    """SQLite-backed cue index. Dependencies are the existing Atelier stores."""

    def __init__(self, config: dict, moment_store: Any, bucket_mgr: Any,
                 raw_event_store: Any, embedding_engine: Any, dehydrator: Any):
        self.config = config or {}
        self.moment_store = moment_store
        self.bucket_mgr = bucket_mgr
        self.raw_event_store = raw_event_store
        self.embedding_engine = embedding_engine
        self.dehydrator = dehydrator
        state_dir = self.config.get("state_dir") or os.path.join(
            os.path.dirname(os.path.abspath(self.config.get("buckets_dir", "buckets"))), "state")
        os.makedirs(state_dir, exist_ok=True)
        self.db_path = os.path.join(state_dir, "assoc_recall.sqlite3")
        self.allowed_root = str(getattr(bucket_mgr, "base_dir", self.config.get("buckets_dir", "")))
        self._cache = None
        self._cache_lock = threading.Lock()
        self._refresh_task = None
        self._features_by_id = {}
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS assoc_cues (
                cue_id TEXT PRIMARY KEY, moment_id TEXT NOT NULL, bucket_id TEXT NOT NULL,
                evidence_version TEXT NOT NULL, cue_text TEXT NOT NULL, quote TEXT NOT NULL,
                cue_type TEXT NOT NULL, vector_json TEXT, model TEXT, created_at REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS assoc_plans (
                cue_id TEXT PRIMARY KEY, moment_id TEXT NOT NULL, evidence_version TEXT NOT NULL,
                plan_state TEXT NOT NULL, plan_quote TEXT NOT NULL, outcome_state TEXT NOT NULL,
                outcome_quote TEXT NOT NULL, outcome_moment_id TEXT NOT NULL,
                outcome_revision TEXT NOT NULL, review_sig TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS assoc_bucket ON assoc_cues(bucket_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS assoc_moment ON assoc_cues(moment_id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS assoc_state (
                moment_id TEXT PRIMARY KEY, evidence_version TEXT NOT NULL,
                indexed_at REAL NOT NULL)""")

    async def _bucket(self, bucket_id: str) -> dict | None:
        try:
            bucket = await self.bucket_mgr.get(bucket_id)
        except Exception:
            return None
        return bucket if isinstance(bucket, dict) else None

    async def _all_buckets(self) -> list[dict]:
        try:
            buckets = await self.bucket_mgr.list_all(include_archive=True)
        except TypeError:
            buckets = await self.bucket_mgr.list_all()
        return [b for b in (buckets or []) if isinstance(b, dict)]

    @staticmethod
    def _eligible(moment: dict, bucket: dict) -> bool:
        if not isinstance(moment, dict) or not isinstance(bucket, dict):
            return False
        text = str(moment.get("text") or "").strip()
        if not text or moment.get("section") in {"feel", "dream", "self", "affect_anchor"}:
            return False
        meta = bucket.get("metadata") if isinstance(bucket.get("metadata"), dict) else {}
        mm = _metadata(moment)
        if meta.get("type") == "feel" or is_self_anchor_bucket(bucket) or meta.get("supersede_retired"):
            return False
        if meta.get("supersede_needs_review") or meta.get("superseded_raw_event_ids"):
            # A mixed bucket cannot be safely partitioned from the supplied APIs.
            return False
        if meta.get("active") is False or meta.get("deprecated") or meta.get("stale") or meta.get("conflict") or meta.get("harmful"):
            return False
        if mm.get("stale") or mm.get("conflict") or mm.get("harmful") or mm.get("self_anchor"):
            return False
        if moment.get("section") not in {"body","original","moment","fact","evidence_context"}:
            return False
        return True

    def _snapshot(self, moment: dict, bucket: dict) -> str:
        meta = dict(bucket.get("metadata") or {})
        # Touch/activation bookkeeping must not invalidate an otherwise current cue.
        for key in ("last_active", "activation_count", "updated_at", "last_accessed", "touch_count"):
            meta.pop(key, None)
        source = _metadata(moment).get("source_ref")
        payload = {
            "moment_id": moment.get("moment_id"), "bucket_id": moment.get("bucket_id"),
            "section": moment.get("section"), "source_id": moment.get("source_id"),
            "version": GENERATOR_VERSION, "embed_model": self.embedding_engine.model if self.embedding_engine else None,
            "text": str(moment.get("text") or ""), "bucket_body": str(bucket.get("content") or ""),
            "source_ref": source, "metadata": meta,
        }
        return _hash(_json(payload))

    async def _fresh_moment(self, moment_id: str) -> dict | None:
        try:
            reader = getattr(self.moment_store, "get_async", None)
            moment = await reader(moment_id) if reader else self.moment_store.get(moment_id)
        except Exception:
            return None
        return moment if isinstance(moment, dict) else None

    async def _retired_source_ids(self):
        reader = getattr(self.raw_event_store, "superseded_ids_async", None)
        return await reader() if reader else self.raw_event_store.superseded_ids()

    def _source_window(self, moment: dict, bucket: dict, quote: str) -> str | None:
        """Use the authorized current bucket, optionally narrowed by source coordinates."""
        try:
            from source_refs import source_ref_window
            window = source_ref_window(moment, body=str(bucket.get("content") or ""), max_chars=1000, context_lines=2)
        except Exception:
            window = ""
        # The live bucket is authoritative even if the saved moment still contains yesterday's text.
        body = str(bucket.get("content") or "")
        if not quote or quote not in body:
            return None
        if quote not in window:
            window = body
        pos = window.index(quote)
        return window[max(0,pos-100):pos+len(quote)+100]


    async def resolve(self, moment_id: str, expected_revision: str | None = None, quote: str | None = None) -> dict | None:
        moment = await self._fresh_moment(str(moment_id or ""))
        if not moment or not moment.get("bucket_id"):
            return None
        bucket = await self._bucket(str(moment["bucket_id"]))
        if not bucket or not self._eligible(moment, bucket):
            return None
        version = self._snapshot(moment, bucket)
        if expected_revision and expected_revision != version:
            return None
        selected_quote = str(quote or "").strip()
        with closing(self._connect()) as conn:
            rows = conn.execute("""SELECT c.quote, p.plan_state, p.plan_quote, p.outcome_state, p.outcome_quote,
                       p.outcome_moment_id, p.outcome_revision FROM assoc_cues c
                       LEFT JOIN assoc_plans p ON p.cue_id=c.cue_id AND p.evidence_version=c.evidence_version
                       WHERE c.moment_id=? AND c.evidence_version=? ORDER BY c.rowid""",
                                (moment_id,version)).fetchall()
        if not rows: return None
        selected = next((r for r in rows if r["quote"]==selected_quote),None) if selected_quote else rows[0]
        if selected is None:return None
        selected_quote=selected["quote"]
        meta = bucket.get("metadata") or {}
        refs = {int(x) for x in meta.get("source_raw_event_ids",[]) or [] if str(x).isdigit()}
        if refs and refs.intersection(await self._retired_source_ids()): return None
        window = self._source_window(moment, bucket, selected_quote) if selected_quote else ""
        if selected_quote and not window:
            return None
        plan_quote = selected["plan_quote"] or ""
        plan_state = "planned" if (selected["plan_state"]=="planned" and plan_quote
                                   and plan_quote in str(moment.get("text") or "")
                                   and plan_quote in str(bucket.get("content") or "")) else "unknown"
        if plan_state!="planned":plan_quote=""
        outcome_state, outcome_quote, outcome_ref = "unknown", "", ""
        oq = selected["outcome_quote"]
        if plan_state=="planned" and selected["outcome_state"] in {"completed","cancelled","rescheduled"} and oq:
            oid=selected["outcome_moment_id"]
            om = moment if oid==moment_id else await self._fresh_moment(oid)
            ob = bucket if om and om["bucket_id"]==bucket["id"] else await self._bucket(om["bucket_id"]) if om else None
            linked = oid == moment_id
            if om and ob and not linked:
                from memory_edges import MemoryEdgeStore
                reader = self.config.get("list_edges_async")
                edges = await reader() if reader else MemoryEdgeStore(self.config).list_edges()
                linked = any(e.get("relation_type") in {"updates","same_event"} and
                             {e.get("source"),e.get("target")}=={bucket["id"],ob["id"]}
                             for e in edges)
            if linked and om and ob and self._eligible(om,ob) and self._snapshot(om,ob)==selected["outcome_revision"]:
                refs={int(v) for v in (ob.get("metadata") or {}).get("source_raw_event_ids",[]) or [] if str(v).isdigit()}
                if (not refs or not refs.intersection(await self._retired_source_ids())) and oq in om["text"] and oq in ob["content"]:
                    outcome_state,outcome_quote,outcome_ref=selected["outcome_state"],oq,oid
        bucket_meta = bucket.get("metadata") if isinstance(bucket.get("metadata"), dict) else {}
        archived = bool(bucket.get("archived")) or str(bucket.get("type") or bucket_meta.get("type") or "").lower() in {"archive", "archived"}
        return {
            "candidate_id": str(moment["moment_id"]), "event_id": str(moment["bucket_id"]),
            "evidence_version": version, "source_window": window, "excerpt": selected_quote,
            "date": _date(moment, bucket), "archived": archived,
            "plan_state": plan_state, "plan_quote": plan_quote[:_MAX_QUOTE_CHARS],
            "outcome_state": outcome_state, "outcome_quote": outcome_quote[:_MAX_QUOTE_CHARS],
            "outcome_source": outcome_ref, "source_revision": version,
        }

    async def _complete(self, prompt: str, *, max_tokens: int = 700) -> str:
        client = getattr(self.dehydrator, "client", None)
        if not client:
            return ""
        model = str(getattr(self.dehydrator, "model", "") or "")
        if not model:
            return ""
        await asyncio.to_thread(reserve,self.db_path,ceiling_usd=8.5,text=prompt,output=max_tokens,retries=3)
        try:
            response = await (client.with_options(max_retries=0) if hasattr(client,"with_options") else client).chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": "Return only valid JSON. Use only the supplied evidence."},
                          {"role": "user", "content": prompt}],
                **(self.dehydrator._completion_options(max_tokens=max_tokens,temperature=0.0) if hasattr(self.dehydrator,"_completion_options") else {"max_tokens":max_tokens,"temperature":0.0}),
            )
            if not response.choices:
                return ""
            return str(response.choices[0].message.content or "")
        except Exception as exc:
            raise RuntimeError("associative cue model unavailable") from exc

    @staticmethod
    def _parse_json(text: str) -> Any:
        value = str(text or "").strip()
        if value.startswith("```"):
            value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I)
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None

    async def _generate_cues(self, moment: dict) -> list[dict]:
        source = str(moment.get("text") or "").strip()[:_MAX_TEXT_CHARS]
        if len(source) < 35:
            return []
        prompt = (
            "Use concise Chinese for cue_text. Create 0 to 4 retrieval cues for this single memory event. Cues are search aids, not facts. "
            "Each item has cue_text, cue_type (situation|question|detail|unfinished), and quote. quote must be an exact, distinct, "
            "contiguous literal excerpt from evidence, at most 220 chars. Also provide plan_state (planned|unknown), plan_quote, "
            "outcome_state (completed|cancelled|rescheduled|unknown), and outcome_quote. Mark planned only for a clear first-person "
            "commitment or scheduled intention, not a wish, hypothetical, question, or date alone. Outcomes require an explicit statement "
            "about that same event; reject negation, hypothetical language, unrelated actions, and date-only evidence. Quotes must be exact "
            "literal excerpts, otherwise use unknown and an empty quote. Old unfinished wording is not proof of a plan. Do not infer motives "
            "or stable traits. Return {\"cues\":[...]}.\nEVIDENCE:\n" + source
        )
        parsed = self._parse_json(await self._complete(prompt))
        raw = parsed.get("cues") if isinstance(parsed, dict) else None
        if not isinstance(raw, list):
            raise ValueError("invalid cue generation response")
        accepted, seen = [], set()
        for item in raw[:_MAX_CUES_PER_MOMENT]:
            if not isinstance(item, dict):
                continue
            cue, quote = str(item.get("cue_text") or "").strip(), str(item.get("quote") or "")
            kind = str(item.get("cue_type") or "").strip()
            if not cue or len(cue) > _MAX_CUE_CHARS or kind not in {"situation", "question", "detail", "unfinished"}:
                continue
            if not quote or len(quote) > _MAX_QUOTE_CHARS or quote not in source or quote in seen:
                continue
            seen.add(quote)
            plan_quote = str(item.get("plan_quote") or "")
            outcome_quote = str(item.get("outcome_quote") or "")
            plan_state = str(item.get("plan_state") or "unknown")
            outcome_state = str(item.get("outcome_state") or "unknown")
            if kind!="unfinished" or plan_state != "planned" or not plan_quote or len(plan_quote) > _MAX_QUOTE_CHARS or plan_quote not in source:
                plan_state, plan_quote = "unknown", ""
            if outcome_state not in {"completed", "cancelled", "rescheduled"} or not outcome_quote or len(outcome_quote) > _MAX_QUOTE_CHARS or outcome_quote not in source:
                outcome_state, outcome_quote = "unknown", ""
            accepted.append({"cue_text": cue, "quote": quote, "cue_type": kind,
                             "plan_state": plan_state, "plan_quote": plan_quote,
                             "outcome_state": outcome_state, "outcome_quote": outcome_quote})
        if not accepted: return []
        from assoc_plans import valid_outcome_quote
        verification = self._parse_json(await self._complete(
            '核对每项的引文是否支持其检索线索。只保留有原文支持的线索。对计划和结果分别复核：计划必须是明确承诺/安排而非愿望或假设；结果必须明确属于同一事件，不能由否定、假设、日期或无关动作推出。只在原文逐字引文准确且结论明确时在计划/结果字段保留状态与引文，否则置为 unknown 和空引文。输出 JSON {"keep":[通过项零基序号],"plans":[{"id":0,"state":"planned","quote":"原文引文"}],"outcomes":[{"id":0,"state":"completed|cancelled|rescheduled","quote":"原文引文"}]}。\n'
            + _json(accepted), max_tokens=220))
        if isinstance(verification, dict) and isinstance(verification.get("keep"), list):
            kept = {n for n in verification["keep"] if type(n) is int}
            plans = {x.get("id"): x for x in verification.get("plans", []) if isinstance(x, dict)}
            outcomes = {x.get("id"): x for x in verification.get("outcomes", []) if isinstance(x, dict)}
            result = []
            for i, row in enumerate(accepted):
                if i not in kept:
                    continue
                item = dict(row)
                plan = plans.get(i, {})
                if plan.get("state") == "planned" and plan.get("quote") == row["plan_quote"] and row["plan_quote"]:
                    item["plan_state"] = "planned"
                else:
                    item["plan_state"], item["plan_quote"] = "unknown", ""
                outcome = outcomes.get(i, {})
                if (item["plan_state"]=="planned" and valid_outcome_quote(row["outcome_quote"]) and row["outcome_quote"]!=item["plan_quote"] and outcome.get("state")==row["outcome_state"] and outcome.get("state") in {"completed", "cancelled", "rescheduled"}
                        and outcome.get("quote") == row["outcome_quote"] and row["outcome_quote"]):
                    item["outcome_state"] = outcome["state"]
                else:
                    item["outcome_state"], item["outcome_quote"] = "unknown", ""
                result.append(item)
            return result
        raise ValueError("invalid verification response")

    async def _embed(self, text: str) -> list[float] | None:
        engine = self.embedding_engine
        if engine is None or not getattr(engine, "enabled", True):
            return None
        try:
            await asyncio.to_thread(reserve,self.db_path,ceiling_usd=8.5,text=text,embedding=True,retries=3)
            vector = await engine._generate_embedding(text, kind="document")
        except Exception:
            return None
        return _finite_vector(vector)

    async def rebuild(self, limit: int, bucket_ids=None):
        # Only one indexer per state directory, including the standalone CLI.
        with open(self.db_path+".index.lock","a") as lock:
            if not try_lock(lock):
                return {"busy":True,"complete":False,"failed":0,"indexed":0}
            return await self._rebuild(limit,bucket_ids)

    async def _rebuild(self, limit: int, bucket_ids=None):
        """Scan source metadata cheaply, charge the limit only to changed events."""
        reader = getattr(self.moment_store, "list_all_async", None)
        moments = await reader() if reader else self.moment_store.list_all()
        selected = set(bucket_ids or ())
        if selected: moments = [m for m in moments if m.get("bucket_id") in selected]
        buckets = {b["id"]:b for b in await self._all_buckets()}
        retired = set(await self._retired_source_ids()) if hasattr(self.raw_event_store, "superseded_ids") else set()
        buckets = {key: bucket for key, bucket in buckets.items() if not any(
            str(ref).isdigit() and int(ref) in retired
            for ref in (bucket.get("metadata") or {}).get("source_raw_event_ids", ()) or ())}
        with closing(self._connect()) as db:
            states=dict(db.execute("SELECT moment_id,evidence_version FROM assoc_state"))
        counters={"scanned":len(moments),"indexed":0,"unchanged":0,"ineligible":0,"failed":0,"stale_removed":0,"complete":True}
        pending=[]
        for m in moments:
            mid=m["moment_id"]; b=buckets.get(m["bucket_id"])
            if not b or not self._eligible(m,b):
                counters["ineligible"]+=1
                continue
            rev=self._snapshot(m,b)
            if states.get(mid)==rev:
                counters["unchanged"]+=1
                continue
            pending.append((m,b,rev))
        if not selected:
            allowed={m["moment_id"] for m in moments if self._eligible(m,buckets.get(m["bucket_id"]))}
            stale=set(states)-allowed
            if stale:
                await asyncio.to_thread(self._remove_stale, stale)
                counters["stale_removed"]=len(stale)
        counters["complete"]=len(pending)<=limit
        async def one(m,b,version):
            mid=m["moment_id"]
            try:
                cues=await self._generate_cues(m)
                staged=[]
                for cue in cues:
                    window=self._source_window(m,b,cue["quote"])
                    if not window:continue
                    vector=await self._embed(cue["cue_text"])
                    if getattr(self.embedding_engine,"enabled",False) and not vector:
                        raise RuntimeError("empty cue vector")
                    cid=_hash(_json([mid,version,cue["cue_type"],cue["quote"]]))
                    staged.append((cid,mid,b["id"],version,cue["cue_text"],cue["quote"],cue["cue_type"],
                                   np.asarray(vector,dtype="<f4").tobytes() if vector else None,getattr(self.embedding_engine,"model",None),time.time(),
                                   cue.get("plan_state", "unknown"), cue.get("plan_quote", ""),
                                   cue.get("outcome_state", "unknown"), cue.get("outcome_quote", "")))
                fresh=await self._bucket(b["id"]); current=await self._fresh_moment(mid)
                if not fresh or not current or not self._eligible(current,fresh) or self._snapshot(current,fresh)!=version:
                    return
                await asyncio.to_thread(self._store_indexed, mid, version, staged)
                counters["indexed"]+=1
            except Exception as exc:
                logger.warning("assoc index skip event=%s kind=%s",mid,type(exc).__name__)
                counters["failed"]+=1
        for i in range(0,min(limit,len(pending)),3):
            await asyncio.gather(*(one(*args) for args in pending[i:min(i+3,limit)]))
        return counters

    def _remove_stale(self, stale):
        rows = [(mid,) for mid in stale]
        with closing(self._connect()) as db, db:
            db.executemany("DELETE FROM assoc_cues WHERE moment_id=?", rows)
            db.executemany("DELETE FROM assoc_plans WHERE moment_id=?", rows)
            db.executemany("""UPDATE assoc_plans SET outcome_state='unknown', outcome_quote='',
                outcome_moment_id='', outcome_revision='', review_sig='' WHERE outcome_moment_id=?""", rows)
            db.executemany("DELETE FROM assoc_state WHERE moment_id=?", rows)

    def _store_indexed(self, mid, version, staged):
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM assoc_cues WHERE moment_id=?",(mid,))
            db.execute("DELETE FROM assoc_plans WHERE moment_id=?",(mid,))
            db.executemany("INSERT OR REPLACE INTO assoc_cues VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (row[:10] for row in staged))
            db.executemany("INSERT OR REPLACE INTO assoc_plans VALUES (?,?,?,?,?,?,?,?,?,?)",
                           ((row[0],mid,version,row[10],row[11],row[12],row[13],mid,version,"")
                            for row in staged if row[10]=="planned"))
            db.execute("INSERT OR REPLACE INTO assoc_state VALUES (?,?,?)",(mid,version,time.time()))

    def _load_features(self):
        # The published tuple is immutable. A refresh pulls vector payloads only
        # for new cue IDs; retirements disappear with the next atomic swap.
        with self._cache_lock, closing(self._connect()) as db, db:
            db.execute("BEGIN")
            marker=tuple(db.execute("SELECT count(*),max(indexed_at) FROM assoc_state").fetchone())
            if self._cache and self._cache[0]==marker:
                return self._cache[1:]
            entries={r[0] for r in db.execute("SELECT cue_id FROM assoc_cues")}
            cache={k:v for k,v in self._features_by_id.items() if k in entries}
            missing=sorted(entries-cache.keys())
            for j in range(0,len(missing),160):
                batch=missing[j:j+160]
                sql="SELECT * FROM assoc_cues WHERE cue_id IN ("+",".join("?" for _ in batch)+")"
                for record in db.execute(sql,batch):
                    row=dict(record); raw=row.pop("vector_json")
                    words=set(_tokens(row["cue_text"]+" "+row["quote"]))
                    vec=None
                    if raw and row["model"]==getattr(self.embedding_engine,"model",None):
                        vec=(np.frombuffer(raw,dtype="<f4") if isinstance(raw,bytes)
                             else np.asarray(json.loads(raw),dtype=np.float32))
                    cache[row["cue_id"]]=(row,words,vec)
            rows=[];terms=[];freq=Counter();ids=[];vecs=[]
            for key in sorted(cache):
                row,words,vec=cache[key]; i=len(rows);rows.append(row);terms.append(words);freq.update(words)
                if vec is not None and vec.ndim==1 and (not vecs or vec.size==vecs[0].size):
                    ids.append(i);vecs.append(vec)
            matrix=np.vstack(vecs) if vecs else None
            self._features_by_id=cache
            self._cache=(marker,rows,terms,freq,ids,matrix)
            return self._cache[1:]

    def warm(self):
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task=asyncio.create_task(asyncio.to_thread(self._load_features))
            self._refresh_task.add_done_callback(
                lambda t: logger.warning("assoc index refresh failed: %s",type(t.exception()).__name__)
                if not t.cancelled() and t.exception() else None)
        return self._refresh_task

    def _rank_directions(self,texts,vectors,limit,features=None):
        """Rank independent semantic directions alongside one unchanged lexical arm."""
        rows,terms,df,ids,matrix=features if features is not None else self._load_features()
        try:
            limit=max(0,int(limit))
            qt=set(_tokens(" ".join(str(text) for text in texts)))
            weights={term:math.log1p(len(rows)/(1+df[term])) for term in qt if term in df}
            lexical=sorted(((sum(weights.get(x,0) for x in term & qt),i)
                            for i,term in enumerate(terms)),reverse=True)
            lexical=[i for val,i in lexical[:limit*3] if val>0 and 0<=i<len(rows)]
        except (TypeError,ValueError,IndexError):
            lexical=[]

        semantic_arms=[]
        if isinstance(matrix,np.ndarray) and matrix.ndim==2 and len(ids)==matrix.shape[0]:
            for vector in vectors or []:
                arm=[]
                query=_finite_vector(vector)
                if query is not None and matrix.shape[1]==len(query):
                    try:
                        scores=np.einsum("ij,j->i",matrix,np.asarray(query,dtype=np.float32))
                        if scores.ndim==1 and len(scores)==len(ids):
                            order=np.argsort(-np.where(np.isfinite(scores),scores,-np.inf),kind="stable")
                            for pos in order:
                                score=float(scores[pos])
                                row_index=ids[int(pos)]
                                if score>.15 and math.isfinite(score) and isinstance(row_index,(int,np.integer)) and 0<=int(row_index)<len(rows):
                                    arm.append(int(row_index))
                                    if len(arm)>=limit*3:break
                    except (TypeError,ValueError,IndexError,OverflowError):
                        arm=[]
                semantic_arms.append(arm)
        arms=semantic_arms+[lexical]
        out=[];seen=set();max_rounds=max((len(arm) for arm in arms),default=0)
        for j in range(max_rounds):
            for arm in arms:
                if j>=len(arm):continue
                r=rows[arm[j]]
                event_id=r.get("bucket_id") if isinstance(r,dict) else None
                if event_id is not None and event_id not in seen:
                    seen.add(event_id);out.append(r)
                if len(out)>=limit*2:return out
        return out

    def _rank(self,texts,vector,limit,features=None):
        """Backward-compatible single-vector ranking entry point."""
        return self._rank_directions(texts,[vector] if vector is not None else [],limit,features)

    async def search(self,texts,limit=12,*,vector=None,allow_embed=True):
        texts=[str(t).strip()[:2000] for t in texts if str(t).strip()][:4]
        if not texts or limit<1:return []
        vectors=[_finite_vector(vector)] if vector is not None else []
        if vector is None and allow_embed and getattr(self.embedding_engine,"enabled",False):
            # Reserve using the exact prepared strings sent to the provider, including
            # its query instruction and truncation, so every batch input is covered.
            directions=[]
            for text in texts[:2]:
                if text not in directions:directions.append(text)
            prepare=getattr(self.embedding_engine,"_prepare_embedding_input",None)
            max_chars=getattr(self.embedding_engine,"max_chars",None)
            if callable(prepare):
                reserved=[prepare(text,kind="query")[:max_chars] if isinstance(max_chars,int) else prepare(text,kind="query") for text in directions]
            else:
                reserved=directions
            try:
                await asyncio.to_thread(reserve,self.db_path,ceiling_usd=8.5,text="\n".join(reserved),embedding=True,retries=3)
                batch=getattr(self.embedding_engine,"generate_query_embeddings",None)
                if callable(batch):
                    vectors=await batch(directions)
                else:
                    # Legacy engines still make only one query request.
                    vectors=[await self.embedding_engine._generate_embedding("\n".join(directions),kind="query")]
            except Exception as exc:
                logger.warning("Associative query embedding batch failed: %s",type(exc).__name__)
                vectors=[]
            vectors=[_finite_vector(item) for item in (vectors or [])]
        published=self._cache
        refreshing=self.warm()
        if published is None:
            if not allow_embed:return []
            features=await asyncio.shield(refreshing)
        else:
            features=published[1:]
        ranked=await asyncio.to_thread(self._rank_directions,texts,vectors,limit,features)
        out=[]
        for r in ranked:
            fresh=await self.resolve(r["moment_id"],r["evidence_version"],quote=r["quote"])
            if fresh:
                out.append({**fresh,"reason":r["cue_text"],"score":1/(1+len(out))})
                if len(out)>=limit:break
        return out
