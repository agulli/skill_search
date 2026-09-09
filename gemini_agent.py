#!/usr/bin/env python
"""A Gemini agent that answers questions using the skill index over MCP.

The index knows about three million agent skills; a model knows about none of
them, and will cheerfully invent a plausible one. This wires the two together:
Gemini decides *what to look up*, the MCP server answers *what is actually
there*, and every claim in the reply traces back to a row in the index.

    export GEMINI_API_KEY=...        # aistudio.google.com/apikey
    python gemini_agent.py "how do I extract tables from a PDF?"
    python gemini_agent.py           # interactive

The tool schemas are read from the MCP server at startup rather than restated
here. Duplicating them would create two descriptions that drift apart, and the
model would be working from the stale one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_MODEL = "gemini-2.5-flash"

SYSTEM = """You help people find and use AI agent skills — SKILL.md files
published on GitHub — from an index of roughly 100,000 of them.

How to work:
- Search the index before answering. You do not know what is in it from memory,
  and a skill you half-remember is a skill you are inventing.
- search_skills returns metadata only. When a result looks right, call
  get_skill to read what it actually says before describing it.
- Cite every skill you rely on as `owner/repo` with its URL, so the user can
  check it.
- Mention the licence when you suggest reusing a skill's text. "unspecified"
  means no licence was declared, which means no permission was granted.
- If the index has nothing good, say so plainly. A bad match presented
  confidently is worse than no match: the user will act on it.

Be concise. Quote the skill's own words for its instructions rather than
paraphrasing them into something it does not say."""


def to_declaration(tool):
    """An MCP tool as a Gemini function declaration.

    MCP publishes JSON Schema and Gemini accepts it directly through
    `parameters_json_schema`, so no translation layer is needed — and none
    should be invented, since a hand-rolled converter is exactly where the two
    descriptions would start to diverge.
    """
    from google.genai import types

    # MCP 2.x exposes `input_schema`; 1.x used `inputSchema`. Accept either so
    # the agent is not pinned to one SDK generation.
    schema = (getattr(tool, "input_schema", None)
              or getattr(tool, "inputSchema", None)
              or {"type": "object", "properties": {}})
    return types.FunctionDeclaration(
        name=tool.name,
        description=tool.description or "",
        parameters_json_schema=schema,
    )


async def run(prompt: str | None, db: str, model: str, max_steps: int = 8) -> int:
    from google import genai
    from google.genai import types
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        print("set GEMINI_API_KEY (get one free at aistudio.google.com/apikey)",
              file=sys.stderr)
        return 1

    client = genai.Client(api_key=key)
    params = StdioServerParameters(
        command=sys.executable, args=[str(Path(__file__).parent / "mcp_server.py"),
                                      "--db", db])

    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as mcp:
            await mcp.initialize()
            listing = await mcp.list_tools()
            tools = [types.Tool(function_declarations=[to_declaration(t)])
                     for t in listing.tools]
            print(f"connected to skill-engine · {len(listing.tools)} tools · {model}\n",
                  file=sys.stderr)

            config = types.GenerateContentConfig(
                system_instruction=SYSTEM,
                tools=tools,
                # The loop below drives tool calls explicitly. Leaving automatic
                # calling on as well would mean two things dispatching the same
                # tools, and the printed trace would no longer match what ran.
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True),
            )

            history: list = []

            async def ask(question: str) -> None:
                history.append(types.Content(role="user",
                                             parts=[types.Part(text=question)]))
                for step in range(max_steps):
                    resp = client.models.generate_content(
                        model=model, contents=history, config=config)
                    cand = (resp.candidates or [None])[0]
                    if cand is None or not cand.content:
                        print("(no response)")
                        return
                    history.append(cand.content)

                    calls = [p.function_call for p in (cand.content.parts or [])
                             if getattr(p, "function_call", None)]
                    if not calls:
                        print((resp.text or "(no answer)").strip())
                        return

                    replies = []
                    for call in calls:
                        args = dict(call.args or {})
                        shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
                        print(f"  → {call.name}({shown})", file=sys.stderr)
                        try:
                            res = await mcp.call_tool(call.name, args)
                            payload = json.loads(res.content[0].text)
                        except Exception as exc:
                            # Handed back to the model rather than raised: a
                            # failed lookup is something it can recover from by
                            # trying different terms, whereas a traceback ends
                            # the conversation.
                            payload = {"error": f"{type(exc).__name__}: {exc}"}
                        replies.append(types.Part.from_function_response(
                            name=call.name, response=payload))
                    history.append(types.Content(role="user", parts=replies))
                print(f"(stopped after {max_steps} tool steps)")

            if prompt:
                await ask(prompt)
                return 0

            print("Ask about agent skills. Ctrl-D to exit.\n", file=sys.stderr)
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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("prompt", nargs="*", help="question; omit for interactive")
    ap.add_argument("--db", default="dist/skills.db")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()
    if not Path(args.db).exists():
        print(f"no index at {args.db}", file=sys.stderr)
        return 1
    return asyncio.run(run(" ".join(args.prompt) or None, args.db, args.model))


if __name__ == "__main__":
    sys.exit(main())
