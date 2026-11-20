"""The real SQLite index, search and readback with fully synthetic providers and files."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from adapters.markdown_memory import AuthorizationError, MarkdownMemory, open_markdown_service
from embedding_engine import EmbeddingEngine
from index_lock import try_lock


QUOTE = "星群你看不到，我替你描一遍。"
PLAN = "下周六我要把蓝边纸卡夹在观星台的本子里。"
CUE = "替缺席的人体验当下事物"
QUERY = "柚子冻真好吃，可惜只能我一个人尝。"
DISTRACTOR = "配方卡写着柚子冻需要冷藏。"
FABRICATED = "星图上贴着一颗钻石。"


class FixtureProvider:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.complete))
        self.embeddings = SimpleNamespace(create=self.embed)
        self.sources = []
        self.queries = []

    async def complete(self, *, messages, **options):
        prompt = messages[-1]["content"]
        if "\nEVIDENCE:\n" in prompt:
            source = prompt.split("\nEVIDENCE:\n", 1)[1]
            self.sources.append(source)
            if QUOTE in source:
                result = {"cues": [
                    {"cue_type": "unfinished", "cue_text": CUE, "quote": QUOTE,
                     "plan_state": "planned", "plan_quote": PLAN},
                    {"cue_type": "detail", "cue_text": CUE, "quote": FABRICATED},
                ]}
            else:
                result = {"cues": [{"cue_type": "detail", "cue_text": "柚子冻保存方法", "quote": DISTRACTOR}]}
        else:
            result = {"keep": [0, 1], "plans": [{"id": 0, "state": "planned", "quote": PLAN}]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])

    async def embed(self, *, input, **options):
        texts = [input] if isinstance(input, str) else input
        if isinstance(input, list):
            self.queries.extend(input)
        vectors = [[1.0, 0.0] if text in (CUE, QUERY) else [0.0, 1.0] for text in texts]
        return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=v) for i, v in enumerate(vectors)])


def validation(candidates, **options):
    return {"schema_version": "assoc.v1", "scope": {
        "session_id": "fixture-session", "branch_id": "fixture-branch", "user_message_id": "fixture-turn",
        "message_version": "1", "request_id": "fixture-request", "last_assistant_ids": [],
    }, "candidates": candidates, **options}


class MarkdownRecallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.source = root / "source"
        self.source.mkdir()
        self.original = self.source / "night.md"
        self.text = "# 鹭湾观星台\n海风翻动手边的纸。\n" + QUOTE + "\n" + PLAN + "\n"
        self.original.write_bytes(self.text.encode("utf-8"))
        (self.source / "recipe.md").write_text(
            "# 配方试做\n这次记录的是两种温度的对照实验，整理完后将结果写进食谱。\n" + DISTRACTOR,
            encoding="utf-8",
        )
        (self.source / "unlisted.md").write_text("未获授权的合成记录。" + self.text, encoding="utf-8")
        self.manifest = root / "allow.json"
        self.grant("night.md", "recipe.md")
        self.memory = MarkdownMemory(self.source, self.manifest)
        self.state = root / "state"
        self.provider = FixtureProvider()
        self.model = SimpleNamespace(client=self.provider, model="fixture-cues")
        self.embeddings = EmbeddingEngine(self.provider, "fixture-vectors")

    def grant(self, *files):
        replacement = self.manifest.with_suffix(".tmp")
        replacement.write_text(json.dumps({"files": files}), encoding="utf-8")
        replacement.replace(self.manifest)

    def service(self, state_dir=None):
        return open_markdown_service(self.memory, state_dir=state_dir or self.state,
                                     embeddings=self.embeddings, model=self.model)

    async def test_build_recall_verify_and_revoke(self):
        async with self.service() as (index, _):
            with open(index.db_path + ".index.lock", "a") as lock:
                self.assertTrue(try_lock(lock))
                self.assertTrue((await index.rebuild(limit=12))["busy"])
            report = await index.rebuild(limit=12)
            self.assertEqual((2, 2, 0, True), tuple(report[k] for k in ("scanned", "indexed", "failed", "complete")))
            self.assertEqual(len(self.provider.sources), 2)
            self.assertTrue(all("未获授权" not in source for source in self.provider.sources))
            with closing(sqlite3.connect(index.db_path)) as db:
                self.assertCountEqual(db.execute("SELECT quote, length(vector_json) FROM assoc_cues"),
                                      [(QUOTE, 8), (DISTRACTOR, 8)])
                self.assertEqual(db.execute("SELECT plan_quote FROM assoc_plans").fetchall(), [(PLAN,)])

        async with self.service() as (index, recall):
            await index.warm()
            lexical = await index.search([QUERY], allow_embed=False)
            self.assertEqual([row["excerpt"] for row in lexical], [DISTRACTOR])
            hit = (await index.search([QUERY]))[0]
            self.assertEqual(hit["excerpt"], QUOTE)
            self.assertIn(hit["source_window"], self.text)
            self.assertIsNone(await index.resolve(hit["candidate_id"], hit["evidence_version"], FABRICATED))
            row, = (await recall.validate(validation([hit])))["candidates"]
            self.assertEqual((row["excerpt"], row["plan_quote"]), (QUOTE, PLAN))
            recheck = validation([row], recheck_only=True)
            self.assertEqual(len((await recall.validate(recheck))["candidates"]), 1)

            # An edit invalidates evidence versions even when the quote survives.
            self.original.write_bytes(self.text.replace("海风", "晨风").encode("utf-8"))
            self.assertEqual((await recall.validate(recheck))["candidates"], [])
            self.assertEqual((await index.rebuild(limit=12))["indexed"], 1)
            await index.warm()
            refreshed = (await index.search([QUERY]))[0]
            self.assertEqual(refreshed["excerpt"], QUOTE)
            self.assertIn("晨风", refreshed["source_window"])
            self.assertNotEqual(refreshed["evidence_version"], hit["evidence_version"])

            # A broken policy blocks search and revalidation without purging good state.
            self.manifest.write_text("{", encoding="utf-8")
            self.assertEqual(await index.search([QUERY]), [])
            self.assertEqual((await recall.validate(validation([refreshed])))["candidates"], [])
            with self.assertRaises(AuthorizationError):
                await index.rebuild(limit=12)
            self.grant("night.md", "recipe.md")
            row, = (await recall.validate(validation([refreshed])))["candidates"]
            self.grant("recipe.md")
            self.assertTrue(self.original.exists())
            self.assertIsNone(await index.resolve(row["candidate_id"], row["evidence_version"], QUOTE))
            self.assertEqual((await recall.validate(validation([row], recheck_only=True)))["candidates"], [])
            self.assertEqual([row["excerpt"] for row in await index.search([QUERY])], [DISTRACTOR])
            report = await index.rebuild(limit=12)
            self.assertEqual((report["stale_removed"], report["unchanged"], report["failed"]), (1, 1, 0))
            with closing(sqlite3.connect(index.db_path)) as db:
                self.assertEqual(db.execute("SELECT quote FROM assoc_cues").fetchall(), [(DISTRACTOR,)])
                self.assertEqual(db.execute("SELECT count(*) FROM assoc_plans").fetchone(), (0,))
                self.assertEqual(db.execute("SELECT count(*) FROM assoc_state").fetchone(), (1,))

        async with self.service() as (index, recall):
            self.assertEqual([row["excerpt"] for row in await index.search([QUERY])], [DISTRACTOR])
            self.assertEqual((await recall.validate(validation([refreshed])))["candidates"], [])

    async def test_state_cannot_overlap_source(self):
        with self.assertRaises(ValueError):
            async with self.service(self.source / "state"):
                self.fail("opened state inside the Markdown directory")

    async def test_literal_quote_beats_conflicting_vector_and_respects_revocation(self):
        async with self.service() as (index, _):
            self.assertEqual((await index.rebuild(limit=12))["indexed"], 2)
            await index.warm()

            query = f"「{QUOTE.rstrip('。')}」"
            hits = await index.search([query], limit=2, vector=[0.0, 1.0])
            self.assertEqual([row["excerpt"] for row in hits], [QUOTE, DISTRACTOR])
            source = await self.memory.get(hits[0]["event_id"])
            self.assertEqual(source["metadata"]["origin"], "night.md")
            self.assertIn(hits[0]["excerpt"], source["content"])
            self.assertEqual(await index.search([query], limit=1), hits[:1])
            self.assertEqual(self.provider.queries, [])

            self.grant("recipe.md")
            remaining, = await index.search([query], limit=1, vector=[0.0, 1.0])
            self.assertEqual(remaining["excerpt"], DISTRACTOR)
            self.assertNotEqual(remaining["event_id"], hits[0]["event_id"])
