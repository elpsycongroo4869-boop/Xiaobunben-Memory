"""Offline contract and permission tests. Run python -m unittest tests.test_markdown_memory."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from adapters.markdown_memory import AuthorizationError, MarkdownMemory


class MarkdownMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "notes").mkdir()
        (self.root / "notes" / "story.md").write_text("原话：我帮你看月亮。", encoding="utf-8")
        (self.root / "notes" / "private.md").write_text("private", encoding="utf-8")
        self.manifest = self.root / "allow.json"
        self.manifest.write_text(json.dumps({"files": ["notes/story.md"]}), encoding="utf-8")
        self.store = MarkdownMemory(self.root, self.manifest)

    def test_allowlisted_readback_and_ports(self):
        moments = self.store.moments.list_all()
        self.assertEqual(len(moments), 1)
        self.assertIn("我帮你看月亮", moments[0]["text"])
        self.assertEqual(self.store.moments.get(moments[0]["moment_id"]), moments[0])
        bucket = asyncio.run(self.store.get(moments[0]["bucket_id"]))
        self.assertEqual(bucket["content"], moments[0]["text"])
        self.assertEqual(self.store.raw_events.superseded_ids(), set())

    def test_unlisted_files_not_read(self):
        self.assertEqual(len(asyncio.run(self.store.list_all())), 1)
        self.assertNotIn("private", str(self.store.moments.list_all()))

    def test_edits_and_revocation(self):
        row = self.store.moments.list_all()[0]
        (self.root / "notes" / "story.md").write_text("更新后的原文", encoding="utf-8")
        self.assertEqual(self.store.moments.get(row["moment_id"])["text"], "更新后的原文")
        self.manifest.write_text('{"files":[]}', encoding="utf-8")
        self.assertIsNone(self.store.moments.get(row["moment_id"]))

    def test_missing_source_fails_closed(self):
        (self.root / "notes" / "story.md").unlink()
        with self.assertRaises(AuthorizationError):
            self.store.moments.list_all()

    def test_parent_escape_rejected(self):
        self.manifest.write_text('{"files":["../outside.md"]}', encoding="utf-8")
        with self.assertRaises(AuthorizationError):
            self.store.moments.list_all()

    def test_symlink_rejected(self):
        (self.root / "notes" / "shortcut.md").symlink_to(self.root / "notes" / "story.md")
        self.manifest.write_text('{"files":["notes/shortcut.md"]}', encoding="utf-8")
        with self.assertRaises(AuthorizationError):
            self.store.moments.list_all()


if __name__ == "__main__":
    unittest.main()
