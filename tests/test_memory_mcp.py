"""Exercise the real MCP stdio server process with a changing demo allowlist."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import unittest

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from host_example.markdown_demo import DISTRACTOR, QUERY, QUOTE
from memory_mcp import LocalConfig


class MemoryMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_discovery_search_and_live_revocation(self):
        await self.exercise_stdio("2026-07-28", "2026-07-28")

    async def test_stdio_legacy_initialization_and_search(self):
        await self.exercise_stdio("legacy", "2025-11-25")

    async def exercise_stdio(self, mode, version):
        repo = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "allow.json"
            manifest.write_text('{"files":["night.md","recipe.md"]}', encoding="utf-8")
            command = StdioServerParameters(command=sys.executable, cwd=repo, args=[
                "-B", "-m", "memory_mcp", "--demo-manifest", str(manifest),
            ])

            async with asyncio.timeout(20), Client(command, mode=mode) as client:
                self.assertEqual(client.protocol_version, version)
                if mode == "legacy":
                    self.assertEqual(client.server_info.name, "Xiaobunben Memory Demo")
                tool, = (await client.list_tools()).tools
                self.assertEqual(tool.name, "search_memory")
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)
                self.assertEqual(tool.input_schema["properties"]["query"]["maxLength"], 500)

                async def query(text):
                    result = await client.call_tool(tool.name, {"query": text})
                    self.assertFalse(result.is_error)
                    return result.structured_content["matches"]

                hit, = await query(QUOTE.rstrip("。"))
                self.assertEqual(set(hit), {"source_id", "excerpt", "evidence_version"})
                self.assertEqual(hit["excerpt"], QUOTE)
                self.assertEqual(hit["source_id"], "md-" + hashlib.sha256(b"night.md").hexdigest()[:24])
                self.assertRegex(hit["evidence_version"], r"^[0-9a-f]{64}$")
                keyword_hit, = await query("柚子冻")
                self.assertEqual(keyword_hit["excerpt"], DISTRACTOR)
                self.assertEqual(keyword_hit["source_id"], "md-" + hashlib.sha256(b"recipe.md").hexdigest()[:24])
                self.assertEqual(await query(QUERY), [keyword_hit])
                self.assertEqual(await query("行星齿轮润滑参数"), [])
                invalid = await client.call_tool(tool.name, {"query": "x" * 501})
                self.assertTrue(invalid.is_error)

                replacement = manifest.with_suffix(".tmp")
                replacement.write_text(json.dumps({"files": ["recipe.md"]}), encoding="utf-8")
                replacement.replace(manifest)
                self.assertEqual(await query(QUOTE), [])
                self.assertEqual(await query(QUERY), [keyword_hit])
                manifest.write_text("{", encoding="utf-8")
                self.assertEqual(await query(QUERY), [])

    async def test_demo_streamable_http(self):
        repo = Path(__file__).resolve().parents[1]
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        with TemporaryDirectory() as directory:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-m", "memory_mcp", "--demo-http", str(port), cwd=repo,
                env={**os.environ, "TMP": directory, "TEMP": directory, "TMPDIR": directory},
            )
            try:
                async with asyncio.timeout(20):
                    while True:
                        try:
                            _, writer = await asyncio.open_connection("127.0.0.1", port)
                        except ConnectionRefusedError:
                            self.assertIsNone(process.returncode, "MCP server exited before listening")
                            await asyncio.sleep(0.05)
                        else:
                            writer.close()
                            await writer.wait_closed()
                            break
                    async with httpx2.AsyncClient(trust_env=False) as http:
                        transport = streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http)
                        async with Client(transport, mode="2026-07-28") as client:
                            tool, = (await client.list_tools()).tools
                            result = await client.call_tool(tool.name, {"query": QUERY})
                            match, = result.structured_content["matches"]
                            self.assertEqual(match["excerpt"], DISTRACTOR)
            finally:
                if process.returncode is None:
                    process.terminate()
                await process.wait()

    def test_local_models_never_default_to_a_remote_api(self):
        example = Path(__file__).resolve().parents[1] / "host_example" / "memory-local.example.json"
        config = LocalConfig.model_validate_json(example.read_bytes()).model_dump()
        for url in ("https://api.openai.com/v1", "http://localhost:11434/v1",
                    "http://127.0.0.1:11434@outside.example/v1", "http://127.0.0.1:11434/v1?x=1"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                LocalConfig.model_validate({**config, "model_url": url})
