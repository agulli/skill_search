#!/usr/bin/env python
"""Autonomous Gemini agent that queries the skill-engine index via MCP.

This script connects the Google GenAI SDK (Gemini 2.5 Flash) to the skill-engine
Model Context Protocol (MCP) server, allowing the model to search and retrieve
grounded agent skills from the local index.

Usage:
    export GEMINI_API_KEY="your-api-key"   # Get one from https://aistudio.google.com/apikey
    python gemini_agent.py "How do I extract tables from a PDF?"
    python gemini_agent.py                 # Interactive shell
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_MODEL = "gemini-2.5-flash"

SYSTEM_INSTRUCTION = """You help users discover, evaluate, and implement AI agent skills (SKILL.md specifications) from an index of over 100,000 repositories.

Operational Guidelines:
1. Search the index before answering. Retrieve verified skills rather than hallucinating paths or configurations.
2. The search_skills tool returns summary metadata. When a relevant skill is identified, call get_skill to read its full instructions.
3. Cite every referenced skill using its owner/repo name along with its full URL.
4. Verify and state the license when recommending reuse.
5. If no matching skill is found in the corpus, state so clearly and recommend alternative keyword queries.
6. Quote original skill instructions accurately when providing actionable execution steps.
"""


def to_declaration(tool: Any) -> Any:
    """Converts an MCP tool definition to a Gemini FunctionDeclaration.

    Args:
        tool: An MCP Tool object exposing name, description, and schema.

    Returns:
        google.genai.types.FunctionDeclaration compatible with Gemini API.
    """
    from google.genai import types

    # Support MCP 2.x (input_schema) and 1.x (inputSchema)
    schema = (
        getattr(tool, "input_schema", None)
        or getattr(tool, "inputSchema", None)
        or {"type": "object", "properties": {}}
    )
    return types.FunctionDeclaration(
        name=tool.name,
        description=tool.description or "",
        parameters_json_schema=schema,
    )


async def run(prompt: str | None, db: str, model: str, max_steps: int = 8) -> int:
    """Initializes the MCP client and executes the Gemini tool-dispatch loop.

    Args:
        prompt: Initial question or query. If None, launches interactive mode.
        db: Path to the SQLite skills database.
        model: Gemini model identifier (e.g., gemini-2.5-flash).
        max_steps: Maximum reasoning and tool-calling iterations per turn.

    Returns:
        Exit code (0 on success, 1 on failure).
    """
    from google import genai
    from google.genai import types
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        print(
            "Error: Set GEMINI_API_KEY environment variable (obtainable at https://aistudio.google.com/apikey)",
            file=sys.stderr,
        )
        return 1

    client = genai.Client(api_key=key)
    server_script = str(Path(__file__).parent / "mcp_server.py")
    params = StdioServerParameters(command=sys.executable, args=[server_script, "--db", db])

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as mcp:
            await mcp.initialize()
            listing = await mcp.list_tools()
            tools = [
                types.Tool(function_declarations=[to_declaration(t)])
                for t in listing.tools
            ]
            print(
                f"Connected to skill-engine MCP server · {len(listing.tools)} tools loaded · Model: {model}\n",
                file=sys.stderr,
            )

            config = types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                tools=tools,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )

            history: list[Any] = []

            async def ask(question: str) -> None:
                history.append(types.Content(role="user", parts=[types.Part(text=question)]))
                for step in range(max_steps):
                    resp = client.models.generate_content(
                        model=model, contents=history, config=config
                    )
                    candidate = (resp.candidates or [None])[0]
                    if candidate is None or not candidate.content:
                        print("(No response generated)")
                        return
                    history.append(candidate.content)

                    calls = [
                        part.function_call
                        for part in (candidate.content.parts or [])
                        if getattr(part, "function_call", None)
                    ]
                    if not calls:
                        print((resp.text or "(No answer)").strip())
                        return

                    replies = []
                    for call in calls:
                        args = dict(call.args or {})
                        arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
                        print(f"  → Invoking tool: {call.name}({arg_str})", file=sys.stderr)
                        try:
                            res = await mcp.call_tool(call.name, args)
                            payload = json.loads(res.content[0].text)
                        except Exception as exc:
                            payload = {"error": f"{type(exc).__name__}: {exc}"}
                        replies.append(
                            types.Part.from_function_response(name=call.name, response=payload)
                        )
                    history.append(types.Content(role="user", parts=replies))
                print(f"(Reached maximum tool step limit: {max_steps})")

            if prompt:
                await ask(prompt)
                return 0

            print("Interactive Gemini Agent session. Type query or press Ctrl-D to exit.\n", file=sys.stderr)
            while True:
                try:
                    q = input("> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return 0
                if q:
                    await ask(q)
                    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Gemini agent powered by skill-engine MCP")
    parser.add_argument("prompt", nargs="*", help="Question to ask; omit for interactive mode")
    parser.add_argument("--db", default="dist/skills.db", help="Path to skills database")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model name")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"Error: Database index not found at {args.db}", file=sys.stderr)
        return 1

    prompt_str = " ".join(args.prompt) if args.prompt else None
    return asyncio.run(run(prompt_str, args.db, args.model))


if __name__ == "__main__":
    sys.exit(main())
