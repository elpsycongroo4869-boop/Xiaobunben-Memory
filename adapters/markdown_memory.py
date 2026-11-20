"""Explicitly authorized, read-only Markdown adapter for Callback_DLC.

No ChatGPT account scraping, GitHub publishing of private memories, or implicit
filesystem discovery. The host exports selected files into a local directory.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath


class AuthorizationError(RuntimeError):
    """The configured source collection cannot be safely enumerated."""


class MarkdownMemory:
    """Present an allowlisted Markdown directory as moments/buckets/raw_events ports."""

    def __init__(self, root: str | Path, manifest: str | Path):
        self.base_dir = Path(root).expanduser().resolve(strict=True)
        self.manifest = Path(manifest).expanduser().resolve(strict=True)
        if not self.base_dir.is_dir():
            raise AuthorizationError("root must be a directory")
        if not self.manifest.is_file():
            raise AuthorizationError("manifest must be a file")
        self.moments = _Moments(self)
        self.raw_events = _RawEvents()

    def _entries(self):
        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AuthorizationError("cannot read authorization manifest") from exc
        if not isinstance(data, dict) or set(data) != {"files"} or not isinstance(data["files"], list):
            raise AuthorizationError('manifest must be {"files": ["relative/file.md", ...]}')
        paths = data["files"]
        if len(paths) != len(set(map(str, paths))):
            raise AuthorizationError("duplicate entries")
        if len(paths) > 1000:
            raise AuthorizationError("too many entries")
        return [self._entry(p) for p in paths]

    def _entry(self, relative):
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise AuthorizationError("invalid path")
        path = PurePosixPath(relative)
        if path.is_absolute() or any(part in ("..", ".") for part in relative.split("/")) or path.suffix.lower() != ".md":
            raise AuthorizationError("only relative Markdown paths allowed")
        candidate = self.base_dir.joinpath(*path.parts)
        if candidate.is_symlink() or any(p.is_symlink() for p in candidate.parents if p != self.base_dir and self.base_dir in p.parents):
            raise AuthorizationError("symlink sources are forbidden")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.base_dir)
            if not resolved.is_file() or resolved.stat().st_size > 1024 * 1024:
                raise AuthorizationError("not a small regular Markdown file")
            text = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            raise AuthorizationError("source missing, unreadable, or outside root") from exc
        key = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:24]
        return {"id": "md-" + key, "relative": relative, "text": text}

    def _find(self, bucket_id):
        for row in self._entries():
            if row["id"] == bucket_id:
                return row
        return None

    async def list_all(self, include_archive=True):
        return [self._bucket(entry) for entry in self._entries()]

    async def get(self, bucket_id):
        entry = self._find(bucket_id)
        return self._bucket(entry) if entry else None

    @staticmethod
    def _bucket(entry):
        return {"id": entry["id"], "content": entry["text"], "type": "dynamic",
                "metadata": {"type": "dynamic", "origin": entry["relative"],
                             "source_raw_event_ids": []}}


class _Moments:
    def __init__(self, memory):
        self.memory = memory

    def list_all(self):
        return [self._moment(row) for row in self.memory._entries()]

    def get(self, moment_id):
        if not isinstance(moment_id, str) or not moment_id.endswith(":body"):
            return None
        row = self.memory._find(moment_id[:-5])
        return self._moment(row) if row else None

    @staticmethod
    def _moment(row):
        return {"moment_id": row["id"] + ":body", "bucket_id": row["id"],
                "section": "body", "source_id": row["relative"],
                "text": row["text"]}


class _RawEvents:
    @staticmethod
    def superseded_ids():
        return set()
