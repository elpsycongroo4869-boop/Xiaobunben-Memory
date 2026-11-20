"""Read-only Callback_DLC MCP server. Network transport serves built-in fiction only."""
import argparse
import asyncio
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from typing import Annotated, TypedDict

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from pydantic import BaseModel, ConfigDict, Field, field_validator

from adapters.markdown_memory import MarkdownMemory, open_markdown_service
from embedding_engine import EmbeddingEngine
from host_example.markdown_demo import DemoClient, write_demo
from providers import RecallModel


class LocalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Path
    manifest: Path
    state: Path
    model_url: str
    cue_model: str = Field(min_length=1)
    embedding_model: str = Field(min_length=1)

    @field_validator("model_url")
    @classmethod
    def require_loopback(cls, url):
        match = re.fullmatch(r"http://(?:127\.0\.0\.1|\[::1\]):([0-9]{1,5})/v1/?", url)
        if not match or not 0 < int(match[1]) <= 65535:
            raise ValueError("model_url must be a literal loopback HTTP endpoint ending in /v1")
        return url


class Evidence(TypedDict):
    source_id: str
    excerpt: str
    evidence_version: str


class SearchResult(TypedDict):
    matches: list[Evidence]


def build_server(index, *, demo=False):
    name = "Xiaobunben Memory Demo" if demo else "Xiaobunben Memory Local"
    server = MCPServer(name, version="0.1", log_level="WARNING")

    @server.tool(title="查找关联记忆", annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ))
    async def search_memory(query: Annotated[str, Field(min_length=1, max_length=500)]) -> SearchResult:
        """Search the authorized memory collection for a related past event and return verified evidence."""
        results = await index.search([query], limit=1)
        if demo:
            print(f"search_memory demo completed: {len(results)} match(es)", file=sys.stderr, flush=True)
        return {"matches": [{"source_id": row["event_id"], "excerpt": row["excerpt"],
                             "evidence_version": row["evidence_version"]} for row in results]}

    return server


async def serve(memory, state, client, cue_model, embedding_model, *, demo=False, demo_http=None):
    async with open_markdown_service(memory, state_dir=state,
                                    embeddings=EmbeddingEngine(client, embedding_model),
                                    model=RecallModel(client, cue_model)) as (index, _):
        report = await index.rebuild(limit=12)
        if report.get("failed"):
            raise RuntimeError("memory index build failed; check the configured models")
        await index.warm()
        server = build_server(index, demo=demo)
        if demo_http is None:
            await server.run_stdio_async()
        else:
            await server.run_streamable_http_async(host="127.0.0.1", port=demo_http, json_response=True,
                                                   stateless_http=True, max_request_body_size=16384)


async def run(args):
    if args.local_config is not None:
        config_path = args.local_config.resolve(strict=True)
        config = LocalConfig.model_validate_json(config_path.read_bytes())
        parent = config_path.parent
        memory = MarkdownMemory(parent / config.source, parent / config.manifest)
        async with AsyncOpenAI(base_url=config.model_url, api_key="local-only", max_retries=0,
                               timeout=30, http_client=DefaultAsyncHttpxClient(
                                   trust_env=False, follow_redirects=False)) as client:
            await serve(memory, parent / config.state, client, config.cue_model, config.embedding_model)
        return

    with TemporaryDirectory(prefix="xiaobunben-mcp-") as directory:
        root = Path(directory)
        source, manifest = write_demo(root)
        memory = MarkdownMemory(source, args.demo_manifest if args.demo_manifest is not None else manifest)
        await serve(memory, root / "state", DemoClient(), "demo-cues", "demo-vectors",
                    demo=True, demo_http=args.demo_http)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local-config", type=Path, help="private local collection setup, for trusted local clients")
    mode.add_argument("--demo-http", type=int, metavar="PORT", help="serve built-in demo at 127.0.0.1:PORT/mcp")
    parser.add_argument("--demo-manifest", type=Path, help="external allowlist for revoking built-in demo files")
    args = parser.parse_args()
    if args.demo_manifest is not None and args.local_config is not None:
        parser.error("--demo-manifest applies only to the built-in demo")
    if args.demo_http is not None and not 1 <= args.demo_http <= 65535:
        parser.error("--demo-http requires a port between 1 and 65535")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
