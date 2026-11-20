"""Conservative, revision-aware plan state reconciliation for associative cues."""
from __future__ import annotations

import hashlib
import json
import asyncio
import logging
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from index_lock import try_lock


_STATES = {"completed", "cancelled", "rescheduled"}
_BATCH = 6
_MAX_QUOTE = 220
_REVIEW_VERSION = "windows-v2:"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _date(index, moment, bucket):
    try:
        from assoc_index import _date as date_of
        return date_of(moment, bucket)
    except Exception:
        meta = bucket.get("metadata") or {}
        return str(meta.get("date") or moment.get("created_at") or "")[:40]


def _load_review_rows(index):
    with closing(index._connect()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS assoc_plans (
            cue_id TEXT PRIMARY KEY, moment_id TEXT, evidence_version TEXT,
            plan_state TEXT, plan_quote TEXT, outcome_state TEXT, outcome_quote TEXT,
            outcome_moment_id TEXT, outcome_revision TEXT, review_sig TEXT)""")
        conn.execute("DELETE FROM assoc_plans WHERE NOT EXISTS (SELECT 1 FROM assoc_cues c WHERE c.cue_id=assoc_plans.cue_id)")
        cues = [dict(r) for r in conn.execute(
            "SELECT cue_id,moment_id,bucket_id,evidence_version,quote,cue_type FROM assoc_cues "
            "WHERE cue_type='unfinished' ORDER BY rowid")]
        stored = {str(r["cue_id"]): dict(r) for r in conn.execute("SELECT * FROM assoc_plans")}
        return cues, stored


def _moments(index):
    try:
        rows = index.moment_store.list_all()
    except TypeError:
        rows = index.moment_store.list_all(limit=10000)
    grouped = defaultdict(list)
    for row in rows or []:
        if isinstance(row, dict) and row.get("bucket_id"):
            grouped[str(row["bucket_id"])].append(row)
    return grouped


def _edge_candidates(index, cue, buckets, moments_by_bucket, edges):
    """Return a bounded, deterministic set of later sources connected by explicit edges."""
    origin = str(cue.get("bucket_id") or "")
    origin_bucket = buckets.get(origin)
    origin_moment = next((m for m in moments_by_bucket.get(origin, [])
                          if str(m.get("moment_id")) == str(cue.get("moment_id"))), None)
    if not origin_bucket or not origin_moment:
        return []
    start = _date(index, origin_moment, origin_bucket)
    found = {}
    for edge in edges:
        if edge.get("relation_type") not in {"same_event", "updates"}:
            continue
        source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
        if source == origin:
            later_id = target
        elif target == origin:
            later_id = source
        else:
            continue
        bucket = buckets.get(later_id)
        if not bucket:
            continue
        for moment in moments_by_bucket.get(later_id, []):
            try:
                if not index._eligible(moment, bucket):
                    continue
            except Exception:
                continue
            date = _date(index, moment, bucket)
            if not start or not date or date <= start:
                continue
            text = str(moment.get("text") or "").strip()
            if not text:
                continue
            # Only bounded event evidence enters the model. A proposed quote is
            # checked against this excerpt and the live source window below.
            excerpt = text[:700]
            revision = index._snapshot(moment, bucket)
            item = {"moment_id": str(moment.get("moment_id")), "bucket_id": later_id,
                    "revision": revision, "date": date, "excerpt": excerpt,
                    "moment": moment, "bucket": bucket}
            found[str(moment.get("moment_id"))] = item
    return sorted(found.values(), key=lambda x: (x["date"], x["moment_id"])) if len(found)<=2 else []


def _review_signature(cue, own_revision, candidates, edges):
    edge_facts = sorted((str(e.get("source")), str(e.get("target")),
                         str(e.get("relation_type")), str(e.get("created_at") or ""),
                         str(e.get("reason") or "")) for e in edges
                        if e.get("relation_type") in {"same_event", "updates"}
                        and str(cue.get("bucket_id")) in {str(e.get("source")), str(e.get("target"))})
    revisions = [(x["moment_id"], x["revision"]) for x in candidates]
    return _hash(_json([cue.get("cue_id"), own_revision, edge_facts, revisions]))


def _quote_window(index, moment, bucket, quote):
    quote = str(quote or "").strip()
    if not quote or len(quote) > _MAX_QUOTE or quote not in str(moment.get("text") or ""):
        return None
    return index._source_window(moment, bucket, quote)


def valid_outcome_quote(text):
    # An isolated date or a hypothetical cannot itself establish an outcome.
    if not text or re.fullmatch(r"[0-9年月日号星期周一二三四五六七天上下本个昨今明后前 /:.，。-]+",text):
        return False
    return not re.match(r"(?:如果|假如|假设|倘若|计划去|打算去|准备去|希望去|If |I plan to )",text.strip(),re.I)


def _response_items(response):
    rows = response.get("items") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise ValueError("malformed plan review")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) not in (str, int):
            continue
        key = str(row["id"])
        if key in result:
            result[key] = None  # Duplicate answers cannot identify one decision.
            continue
        result[key] = None
        fields = ("plan_state", "plan_quote", "outcome_state", "outcome_quote", "outcome_moment_id")
        if not all(isinstance(row.get(field), str) for field in fields):
            continue
        clean = {field: row[field].strip() for field in fields}
        plan, pq, outcome, oq, oid = clean.values()
        if len(pq) > _MAX_QUOTE or len(oq) > _MAX_QUOTE:
            continue
        if plan not in ("planned", "unknown") or outcome not in _STATES | {"unknown"}:
            continue
        if (plan == "planned") != bool(pq) or (outcome in _STATES) != bool(oq):
            continue
        if (outcome in _STATES) != bool(oid) or (oq and not pq):
            continue
        result[key] = dict(clean, id=key)
    return result


async def _review_batch(index, items):
    evidence = []
    for i,item in enumerate(items):
        evidence.append({"id": str(i), "plan_excerpts": item["plan_windows"],
                         "cue_quote": item["cue"]["quote"],
                         "later_sources": [{"moment_id": x["moment_id"], "revision": x["revision"],
                                            "excerpts": x["windows"]} for x in item["later"]]})
    extract_prompt = (
        "For each item decide whether its original excerpt contains an explicit first-person plan or scheduled intention. "
        "Then identify an explicitly stated outcome of that same event in one supplied later source, if any. "
        "Do not infer from dates, elapsed time, similar topics, or a different event. A link is only a candidate; reject it "
        "unless exact subject/action evidence makes it the same event. Return JSON {items:[{id,plan_state:'planned|unknown',"
        "plan_quote, outcome_state:'completed|cancelled|rescheduled|unknown',outcome_quote,outcome_moment_id}]}. "
        "Use the author's explicit correction when it resolves an earlier statement. Each quote must be contiguous "
        "within one excerpt. For unknown use empty quotes and an empty outcome_moment_id for unknown outcome. Evidence:\n" + _json(evidence))
    extracted = index._parse_json(await index._complete(extract_prompt, max_tokens=1200))
    proposals = _response_items(extracted)
    proposed = [proposals[str(i)] for i in range(len(items)) if proposals.get(str(i))]
    if not proposed:
        return {}
    # An independent second pass checks event identity and quote support. It
    # receives the same bounded evidence and proposed claims, not new material.
    verify_prompt = (
        "Independently verify these proposed plan/outcome claims using only the attached evidence. "
        "A plan needs an explicit commitment in the original excerpt. An outcome needs an explicit statement about that "
        "same event in the named later source; reject merely similar or conflicting events. Quotes must be exact and "
        "contiguous. Return JSON {items:[{id,plan_state:'planned|unknown',plan_quote,outcome_state:'completed|cancelled|"
        "rescheduled|unknown',outcome_quote,outcome_moment_id}]}; use unknown and empty quote for unsupported claims "
        "and an empty outcome_moment_id for unknown outcome.\nEVIDENCE:\n"
        + _json(evidence) + "\nPROPOSALS:\n" + _json(proposed))
    verified = index._parse_json(await index._complete(verify_prompt, max_tokens=1200))
    by_id = _response_items(verified)
    accepted = {}
    for i,item in enumerate(items):
        cue = item["cue"]
        row = by_id.get(str(i))
        proposal = proposals.get(str(i))
        if not row or not proposal:
            continue
        plan_state, plan_quote = row["plan_state"], row["plan_quote"]
        own = item["later"][0]
        if (plan_state!="planned" or proposal.get("plan_state")!="planned" or plan_quote!=proposal.get("plan_quote")
                or not any(plan_quote in window for window in item["plan_windows"])
                or not _quote_window(index, own["moment"], own["bucket"], plan_quote)):
            plan_state, plan_quote = "unknown", ""
        outcome_state, outcome_quote, outcome_id = row["outcome_state"], row["outcome_quote"], row["outcome_moment_id"]
        source = next((x for x in item["later"] if x["moment_id"] == outcome_id), None)
        if (outcome_state not in _STATES or outcome_state != proposal.get("outcome_state")
                or outcome_quote != proposal.get("outcome_quote") or outcome_id != str(proposal.get("outcome_moment_id") or "")
                or not source or not valid_outcome_quote(outcome_quote) or outcome_quote==plan_quote
                or not any(outcome_quote in window for window in source["windows"])
                or not _quote_window(index, source["moment"], source["bucket"], outcome_quote)):
            outcome_state, outcome_quote, outcome_id = "unknown", "", ""
        if plan_state != "planned":
            outcome_state, outcome_quote, outcome_id = "unknown", "", ""
        accepted[str(cue["cue_id"])] = {
            "plan_state": plan_state, "plan_quote": plan_quote,
            "outcome_state": outcome_state, "outcome_quote": outcome_quote,
            "outcome_moment_id": outcome_id,
            "outcome_revision": source["revision"] if outcome_id else ""}
    return accepted


def _commit(index, item, result, graph):
    """Validate against the batch's post-model graph; CAS the derived row in SQLite."""
    cue = item["cue"]
    buckets, moments, by_edge = graph
    edges = by_edge[cue["bucket_id"]]
    bucket = buckets.get(cue["bucket_id"])
    current = next((m for m in moments[cue["bucket_id"]] if str(m.get("moment_id")) == str(cue["moment_id"])), None)
    if not current or not bucket or not index._eligible(current, bucket):
        return False
    candidates = _edge_candidates(index, cue, buckets, moments, edges)
    if _review_signature(cue, cue["evidence_version"], candidates, edges) != item["signature"]:
        return False
    if index._snapshot(current, bucket) != cue["evidence_version"]:
        return False
    if not _quote_window(index, current, bucket, str(cue["quote"])):
        return False
    if result["plan_quote"] and not _quote_window(index, current, bucket, result["plan_quote"]):
        return False
    outcome_id = result["outcome_moment_id"]
    outcome_revision = result["outcome_revision"]
    if outcome_id:
        if outcome_id == str(cue["moment_id"]):
            fresh, fresh_bucket = current, bucket
        else:
            source = next((x for x in candidates if x["moment_id"] == outcome_id), None)
            if not source or source["revision"] != outcome_revision:
                return False
            fresh, fresh_bucket = source["moment"], source["bucket"]
        if not _quote_window(index, fresh, fresh_bucket, result["outcome_quote"]):
            return False
    with closing(index._connect()) as conn, conn:
        # A completed model review should retry from fresh sources if the writer is busy.
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute("BEGIN IMMEDIATE")
        live = conn.execute("SELECT moment_id,bucket_id,evidence_version,quote,cue_type FROM assoc_cues WHERE cue_id=?",
                            (cue["cue_id"],)).fetchone()
        if (not live or str(live["moment_id"]) != str(cue["moment_id"])
                or str(live["evidence_version"]) != str(cue["evidence_version"])
                or live["bucket_id"] != cue["bucket_id"] or live["cue_type"] != "unfinished"
                or str(live["quote"]) != str(cue["quote"])):
            return False
        prior = conn.execute("SELECT * FROM assoc_plans WHERE cue_id=?", (cue["cue_id"],)).fetchone()
        if (dict(prior) if prior else None) != item["old"]:
            return False
        conn.execute("""INSERT INTO assoc_plans
            (cue_id,moment_id,evidence_version,plan_state,plan_quote,outcome_state,outcome_quote,
             outcome_moment_id,outcome_revision,review_sig) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cue_id) DO UPDATE SET moment_id=excluded.moment_id,
            evidence_version=excluded.evidence_version,plan_state=excluded.plan_state,
            plan_quote=excluded.plan_quote,outcome_state=excluded.outcome_state,
            outcome_quote=excluded.outcome_quote,outcome_moment_id=excluded.outcome_moment_id,
            outcome_revision=excluded.outcome_revision,review_sig=excluded.review_sig""",
            (cue["cue_id"], cue["moment_id"], cue["evidence_version"], result["plan_state"],
             result["plan_quote"], result["outcome_state"], result["outcome_quote"], outcome_id,
             outcome_revision, _REVIEW_VERSION + item["signature"]))
        conn.commit()
    return True


async def refresh_plan_states(index, limit=24, *, repair_unknown_ids=None):
    """With explicit cue IDs, revisit only their legacy unknown decisions, once per revision."""
    with open(str(index.db_path)+".plans.lock","a") as lock:
        if not try_lock(lock):
            return {"busy":True,"reviewed":0,"failed":0}
        return await _refresh(index, max(0, int(limit)),
                              None if repair_unknown_ids is None else set(repair_unknown_ids))


async def _review_graph(index):
    from memory_edges import MemoryEdgeStore
    all_buckets = await index._all_buckets()
    raw = getattr(index, "raw_event_store", None)
    read_retired = getattr(raw, "superseded_ids_async", None)
    if read_retired:
        retired = await read_retired()
    else:
        retired = raw.superseded_ids() if hasattr(raw, "superseded_ids") else set()
    buckets = {str(b["id"]): b for b in all_buckets if b.get("id") and not retired.intersection(
        int(v) for v in (b.get("metadata") or {}).get("source_raw_event_ids", []) or [] if str(v).isdigit())}
    moments = await asyncio.to_thread(_moments, index)
    by_edge = defaultdict(list)
    read_edges = index.config.get("list_edges_async")
    edges = await read_edges() if read_edges else MemoryEdgeStore(index.config).list_edges()
    for edge in edges:
        if edge.get("relation_type") in {"same_event", "updates"}:
            for bid in {edge.get("source"), edge.get("target")}:
                by_edge[bid].append(edge)
    return buckets, moments, by_edge


def _review_windows(index, cue_window, own, later, old, repair):
    # Keep disjoint windows separate so no quote can span a gap in the source.
    plan = [cue_window]
    prior = old if old and old["evidence_version"] == own["revision"] and old["moment_id"] == own["moment_id"] else {}
    if prior.get("plan_state") == "planned":
        plan.append(_quote_window(index, own["moment"], own["bucket"], prior["plan_quote"]))
    for source in [own, *later]:
        # In a targeted repair the previous quote offsets have already been erased.
        # Revisit the original cue generator's bounded input range.
        prefix = source["moment"]["text"].strip()[:6000] if repair else source["excerpt"]
        windows = [prefix]
        if source is own:
            if repair:
                plan.append(prefix)
            windows.extend(plan)
        if (prior.get("outcome_state") in _STATES and prior["outcome_moment_id"] == source["moment_id"]
                and prior["outcome_revision"] == source["revision"]):
            windows.append(_quote_window(index, source["moment"], source["bucket"], prior["outcome_quote"]))
        source["windows"] = list(dict.fromkeys(w for w in windows if w))
    return list(dict.fromkeys(w for w in plan if w))


def _invalidate_lost_links(index, batch):
    # A previous link may now be revoked or ambiguous even if the provider is down.
    for item in batch:
        old = item["old"]
        if not old or old["outcome_state"] not in _STATES or old["outcome_moment_id"] == old["moment_id"]:
            continue
        if any(s["moment_id"] == old["outcome_moment_id"] and s["revision"] == old["outcome_revision"]
               for s in item["later"]):
            continue
        with closing(index._connect()) as db, db:
            db.execute("PRAGMA busy_timeout=0")
            changed = db.execute("""UPDATE assoc_plans SET outcome_state='unknown', outcome_quote='',
                outcome_moment_id='', outcome_revision='', review_sig='' WHERE cue_id=:cue_id
                AND evidence_version=:evidence_version AND review_sig=:review_sig
                AND outcome_moment_id=:outcome_moment_id AND outcome_revision=:outcome_revision
                AND outcome_state=:outcome_state AND outcome_quote=:outcome_quote""", old).rowcount
        if changed:
            item["old"] = dict(old, outcome_state="unknown", outcome_quote="", outcome_moment_id="",
                               outcome_revision="", review_sig="")


async def _refresh(index, limit, repair_ids):
    """Backfill/reconcile a bounded set of unfinished cues without embeddings.

    Unknown is persisted with a source/edge signature, so unchanged legacy cues
    are not sent to the model on every turn. Provider and parse failures remain
    retryable because they are never committed.
    """
    counts = {"scanned": 0, "reviewed": 0, "failed": 0, "remaining": 0}
    try:
        cues, stored = await asyncio.to_thread(_load_review_rows, index)
        counts["scanned"] = len(cues)
        if not cues or (repair_ids is not None and not repair_ids):
            return counts
        buckets, moments_by_bucket, by_edge = await _review_graph(index)
        def collect():
            pending = []
            for cue in cues:
                old = stored.get(str(cue["cue_id"]))
                if repair_ids is not None and (cue["cue_id"] not in repair_ids or not old
                        or old["outcome_state"] != "unknown" or not old["review_sig"]
                        or old["review_sig"].startswith(_REVIEW_VERSION)):
                    continue
                bucket = buckets.get(str(cue.get("bucket_id") or ""))
                moment = next((m for m in moments_by_bucket.get(str(cue.get("bucket_id") or ""), [])
                               if str(m.get("moment_id")) == str(cue.get("moment_id"))), None)
                if not bucket or not moment:
                    continue
                try:
                    if not index._eligible(moment, bucket) or index._snapshot(moment, bucket) != cue["evidence_version"]:
                        continue
                    es=by_edge[cue["bucket_id"]]
                    later = _edge_candidates(index, cue, buckets, moments_by_bucket, es)
                    signature = _review_signature(cue, cue["evidence_version"], later, es)
                    if (repair_ids is None and old and old["evidence_version"] == cue["evidence_version"]
                            and old["review_sig"] in (signature, _REVIEW_VERSION + signature)):
                        continue
                    cue_window = _quote_window(index, moment, bucket, cue["quote"])
                    if not cue_window:
                        continue
                    # Also allow explicit later wording in the original event.
                    own={"moment_id":str(moment["moment_id"]),"bucket_id":str(bucket["id"]),
                         "revision":cue["evidence_version"],"excerpt":str(moment["text"])[:1200],
                         "moment":moment,"bucket":bucket,"date":_date(index,moment,bucket)}
                    plan_windows = _review_windows(index, cue_window, own, later, old, repair_ids is not None)
                    pending.append({"cue": cue, "old": old, "plan_windows": plan_windows,
                                    "later": [own, *later], "signature": signature})
                except Exception:
                    counts["failed"] += 1
            return pending
        pending=await asyncio.to_thread(collect)
        total=len(pending)
        pending=pending[:limit]
        for start in range(0, len(pending), _BATCH):
            batch = pending[start:start + _BATCH]
            try:
                await asyncio.to_thread(_invalidate_lost_links, index, batch)
                results = await _review_batch(index, batch)
                if not results:
                    counts["failed"] += len(batch)
                    continue
                graph = await _review_graph(index)
            except Exception:
                counts["failed"] += len(batch)
                continue
            for item in batch:
                result = results.get(str(item["cue"]["cue_id"]))
                try:
                    committed = result and await asyncio.to_thread(_commit, index, item, result, graph)
                except sqlite3.OperationalError:
                    logging.getLogger(__name__).debug("plan review commit deferred", exc_info=True)
                    committed = False
                if not committed:
                    counts["failed"] += 1
                else:
                    counts["reviewed"] += 1
        counts["remaining"]=max(0,total-counts["reviewed"])
    except Exception:
        logging.getLogger(__name__).exception("plan review interrupted")
        counts["failed"] += 1
    return counts
