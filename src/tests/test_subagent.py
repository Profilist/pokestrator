#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import agent as agent_module  # noqa: E402
from agent import PokestratorOrchestrator  # noqa: E402
from db import Subagent, get_subagent_by_name, init_db  # noqa: E402

logger = logging.getLogger("pokestrator.test")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a subagent in a debug/streaming mode to observe live Claude SDK events."
    )
    parser.add_argument(
        "task",
        help="Task description to execute (same text you pass to orchestrate).",
    )
    parser.add_argument(
        "--subagent",
        dest="subagent_name",
        default="",
        help="Optional explicit subagent name from DB (for example: auto_google_analytics).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("POKESTRATOR_AGENT_TIMEOUT", "90")),
        help="Timeout seconds for the streamed Claude run.",
    )
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="Print the selected subagent system prompt before running.",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level (default: LOG_LEVEL env or INFO).",
    )
    parser.add_argument(
        "--permission-mode",
        default=os.getenv("POKESTRATOR_PERMISSION_MODE", "bypassPermissions"),
        help="Claude SDK permission mode (default: POKESTRATOR_PERMISSION_MODE or bypassPermissions).",
    )
    parser.add_argument(
        "--tools-preset",
        default=os.getenv("POKESTRATOR_TOOLS_PRESET", "claude_code"),
        help="Claude SDK tools preset (default: POKESTRATOR_TOOLS_PRESET or claude_code).",
    )
    return parser.parse_args()


async def resolve_subagent(
    orchestrator: PokestratorOrchestrator,
    task_description: str,
    subagent_name: str,
) -> tuple[Subagent, str]:
    try:
        await init_db()
    except Exception:
        logger.exception("DB init failed; continuing with route/template resolution")

    if subagent_name:
        subagent = await get_subagent_by_name(subagent_name)
        if subagent is None:
            raise RuntimeError(f"subagent '{subagent_name}' was not found in DB")
        return subagent, "explicit"

    decision = await orchestrator._decide_route(task_description)
    branch = decision["branch"]

    if branch == "match":
        return decision["subagent"], "matched_existing"

    if branch == "template":
        template_name = decision["template_name"]
        subagent = await orchestrator._load_template(template_name)
        if subagent is None:
            raise RuntimeError(f"template '{template_name}' resolved but could not be loaded")
        return subagent, f"template:{template_name}"

    raise RuntimeError(
        "Route decision was build_new (no runnable subagent yet). "
        "Run once through orchestrate first or pass --subagent."
    )


async def stream_subagent_run(
    orchestrator: PokestratorOrchestrator,
    subagent: Subagent,
    task_description: str,
    timeout_seconds: int,
    permission_mode: str,
    tools_preset: str,
) -> str:
    if agent_module.claude_sdk is None:
        raise RuntimeError("claude_agent_sdk is unavailable in this environment")

    options = agent_module.claude_sdk.ClaudeAgentOptions(
        system_prompt=subagent.system_prompt,
        tools={"type": "preset", "preset": tools_preset},
        permission_mode=permission_mode,
    )

    stream = agent_module.claude_sdk.query(prompt=task_description, options=options)
    if asyncio.iscoroutine(stream):
        stream = await stream

    async def consume() -> str:
        event_count = 0
        final_result: str | None = None
        chunks: list[str] = []

        async for event in stream:
            event_count += 1
            print(f"\n[event {event_count}] type={type(event).__name__}")

            text = orchestrator._extract_text(event)
            if text:
                print(f"text: {text}")
                chunks.append(text)

            result = orchestrator._extract_result(event)
            if result:
                print(f"result: {result}")
                final_result = result

        if final_result:
            return final_result.strip()
        if chunks:
            return "\n".join(chunks).strip()
        return ""

    return await asyncio.wait_for(consume(), timeout=timeout_seconds)


async def run() -> int:
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    load_dotenv()

    orchestrator = PokestratorOrchestrator()
    subagent, source = await resolve_subagent(orchestrator, args.task, args.subagent_name)

    print(f"selected_subagent={subagent.name} source={source}")
    print(f"timeout_seconds={args.timeout}")
    print(f"permission_mode={args.permission_mode} tools_preset={args.tools_preset}")
    if args.show_prompt:
        print("\n--- system_prompt ---")
        print(subagent.system_prompt)
        print("--- end system_prompt ---\n")

    try:
        result = await stream_subagent_run(
            orchestrator=orchestrator,
            subagent=subagent,
            task_description=args.task,
            timeout_seconds=args.timeout,
            permission_mode=args.permission_mode,
            tools_preset=args.tools_preset,
        )
    except asyncio.TimeoutError:
        print(
            f"\n[timeout] Claude run exceeded {args.timeout}s. "
            "Increase --timeout or inspect earlier event output."
        )
        return 2
    except Exception as exc:
        logger.exception("debug run failed")
        print(f"\n[error] {exc}")
        return 1

    print("\n=== final_result ===")
    print(result if result else "<empty>")
    print("=== end final_result ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
