"""Explicitly authorized, read-only Markdown adapter for Callback_DLC.

No ChatGPT account scraping, GitHub publishing of private memories, or implicit
filesystem discovery. The host exports selected files into a local directory.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat


_MAX_BYTES = 1024 * 1024


class AuthorizationError(RuntimeError):
    """The configured source collection cannot be safely enumerated."""


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate manifest fields")
    return result


class MarkdownMemory:
    """Present an allowlisted Markdown directory as moments/buckets/raw_events ports."""

    def __init__(self, root: str | Path, manifest: str | Path):
        self.base_dir = Path(root).expanduser().resolve()
        manifest_path = Path(manifest).expanduser().absolute()
        self.manifest = manifest_path.parent.resolve() / manifest_path.name
        if not self.base_dir.is_dir():
            raise AuthorizationError("root must be a directory")
        self._paths()
        self.moments = _Moments(self)
        self.raw_events = _RawEvents()

    @staticmethod
    def _read(path):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or path.is_junction() or info.st_nlink != 1
                or info.st_size > _MAX_BYTES):
            raise AuthorizationError("expected a small regular file without links")
        with path.open("rb") as stream:
            data = stream.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            raise AuthorizationError("file exceeds size limit")
        return data.decode("utf-8")

    def _paths(self):
        try:
            data = json.loads(self._read(self.manifest), object_pairs_hook=_unique_object)
        except (OSError, ValueError) as exc:
            raise AuthorizationError("cannot read authorization manifest") from exc
        if not isinstance(data, dict) or set(data) != {"files"} or not isinstance(data["files"], list):
            raise AuthorizationError('manifest must be {"files": ["relative/file.md", ...]}')
        paths = data["files"]
        if len(paths) > 1000:
            raise AuthorizationError("too many entries")
        entries, seen = {}, set()
        for relative in paths:
            if not isinstance(relative, str) or not relative:
                raise AuthorizationError("invalid path")
            path = PurePosixPath(relative)
            if (path.is_absolute() or path.as_posix() != relative or path.suffix.lower() != ".md"
                    or any(part == ".." or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved()
                           for part in path.parts)
                    or any(c in '\\:<>"|?*' or ord(c) < 32 for c in relative)):
                raise AuthorizationError("only canonical relative Markdown paths allowed")
            canonical = os.path.normcase(relative)
            if canonical in seen:
                raise AuthorizationError("duplicate entries")
            seen.add(canonical)
            try:
                key = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:24]
            except UnicodeError as exc:
                raise AuthorizationError("invalid path encoding") from exc
            entries["md-" + key] = relative
        return entries

    def _entry(self, bucket_id, relative):
        candidate = self.base_dir
        try:
            for part in PurePosixPath(relative).parts:
                candidate = candidate / part
                if candidate.is_symlink() or candidate.is_junction():
                    raise AuthorizationError("linked sources are forbidden")
            resolved = candidate.resolve(strict=True)
            actual = resolved.relative_to(self.base_dir).as_posix()
            if os.path.normcase(actual) != os.path.normcase(relative):
                raise AuthorizationError("source path aliases are forbidden")
            text = self._read(resolved)
        except (OSError, ValueError) as exc:
            raise AuthorizationError("source missing, unreadable, or outside root") from exc
        return {"id": bucket_id, "relative": relative, "text": text}

    def _entries(self):
        return [self._entry(bucket_id, relative) for bucket_id, relative in self._paths().items()]

    def _find(self, bucket_id):
        relative = self._paths().get(bucket_id) if isinstance(bucket_id, str) else None
        return self._entry(bucket_id, relative) if relative is not None else None

    async def list_all(self, include_archive=True):
        entries = await asyncio.to_thread(self._entries)
        return [self._bucket(entry) for entry in entries]

    async def get(self, bucket_id):
        entry = await asyncio.to_thread(self._find, bucket_id)
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

    async def list_all_async(self):
        return await asyncio.to_thread(self.list_all)

    async def get_async(self, moment_id):
        return await asyncio.to_thread(self.get, moment_id)

    @staticmethod
    def _moment(row):
        return {"moment_id": row["id"] + ":body", "bucket_id": row["id"],
                "section": "body", "source_id": row["relative"],
                "text": row["text"]}


class _RawEvents:
    @staticmethod
    def superseded_ids():
        return set()


@asynccontextmanager
async def open_markdown_service(memory, *, state_dir, embeddings, model):
    """Connect one Markdown authorization set to the existing Callback_DLC core."""
    from service import open_service

    state = Path(state_dir).expanduser().resolve()
    root = memory.base_dir
    if state == root or root in state.parents or state in root.parents or state in memory.manifest.parents:
        raise ValueError("use a dedicated state directory separate from sources and manifest")
    async with open_service(state_dir=state, moments=memory.moments, buckets=memory,
                            raw_events=memory.raw_events, embeddings=embeddings, model=model) as service:
        yield service
