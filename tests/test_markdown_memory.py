"""Offline contract and permission tests for the Markdown ports."""
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
        self.assertIsNone(asyncio.run(self.store.get(row["bucket_id"])))
        self.assertEqual(asyncio.run(self.store.list_all()), [])

    def test_preserves_exact_line_endings(self):
        text = "第一行\r\n第二行\n"
        (self.root / "notes" / "story.md").write_bytes(text.encode("utf-8"))
        row, = self.store.moments.list_all()
        self.assertEqual(row["text"], text)
        self.assertEqual(asyncio.run(self.store.get(row["bucket_id"]))["content"], text)

    def test_lookup_survives_unrelated_missing_file_but_enumeration_fails(self):
        row, = self.store.moments.list_all()
        self.manifest.write_text('{"files":["notes/story.md","notes/missing.md"]}', encoding="utf-8")
        self.assertEqual(self.store.moments.get(row["moment_id"]), row)
        with self.assertRaises(AuthorizationError):
            self.store.moments.list_all()

    def test_invalid_manifest_blocks_previously_authorized_ids(self):
        row, = self.store.moments.list_all()
        for invalid in ('{"files":', '{"files":[],"files":["notes/story.md"]}'):
            with self.subTest(invalid=invalid):
                self.manifest.write_text(invalid, encoding="utf-8")
                with self.assertRaises(AuthorizationError):
                    self.store.moments.get(row["moment_id"])
                with self.assertRaises(AuthorizationError):
                    asyncio.run(self.store.get(row["bucket_id"]))

    def test_missing_source_fails_closed(self):
        (self.root / "notes" / "story.md").unlink()
        with self.assertRaises(AuthorizationError):
            self.store.moments.list_all()

    def test_parent_escape_rejected(self):
        self.manifest.write_text('{"files":["../outside.md"]}', encoding="utf-8")
        with self.assertRaises(AuthorizationError):
            self.store.moments.list_all()

    def test_aliases_and_duplicate_paths_rejected(self):
        for paths in (["notes//story.md"], ["notes/./story.md"], ["notes/story.md/"],
                      ["notes/C:story.md"], ["notes/AUX.md"], ["notes /story.md"],
                      ["notes/story.md", "notes/story.md"]):
            with self.subTest(paths=paths):
                self.manifest.write_text(json.dumps({"files": paths}), encoding="utf-8")
                with self.assertRaises(AuthorizationError):
                    self.store.moments.list_all()

    def test_symlink_rejected(self):
        (self.root / "notes" / "shortcut.md").symlink_to(self.root / "notes" / "story.md")
        self.manifest.write_text('{"files":["notes/shortcut.md"]}', encoding="utf-8")
        with self.assertRaises(AuthorizationError):
            self.store.moments.list_all()

    def test_replaced_manifest_link_rejected(self):
        valid = self.root / "previous.json"
        self.manifest.replace(valid)
        self.manifest.symlink_to(valid)
        with self.assertRaises(AuthorizationError):
            self.store.moments.list_all()

    def test_hardlink_and_oversize_source_rejected(self):
        authorized = self.root / "notes" / "story.md"
        authorized.unlink()
        authorized.hardlink_to(self.root / "notes" / "private.md")
        with self.assertRaises(AuthorizationError):
            self.store.moments.list_all()
        authorized.unlink()
        authorized.write_bytes(b"a" * (1024 * 1024 + 1))
        with self.assertRaises(AuthorizationError):
            self.store.moments.list_all()


if __name__ == "__main__":
    unittest.main()
