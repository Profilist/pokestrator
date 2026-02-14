#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent import PokestratorOrchestrator  # noqa: E402
from db import init_db, insert_subagent  # noqa: E402

logger = logging.getLogger("pokestrator.test.orchestrator")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a reusable subagent spec from issue context using the orchestrator "
            "analysis flow."
        )
    )
    parser.add_argument(
        "issue",
        nargs="?",
        default="",
        help="Issue/task description to analyze.",
    )
    parser.add_argument(
        "--issue-file",
        default="",
        help="Path to a text file containing issue context.",
    )
    parser.add_argument(
        "--name-hint",
        default="",
        help="Optional name hint for normalization fallback.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print generated spec as JSON only.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Persist the generated spec to the database.",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level (default: LOG_LEVEL env or INFO).",
    )
    return parser.parse_args()


def load_issue_text(args: argparse.Namespace) -> str:
    if args.issue_file:
        issue_path = Path(args.issue_file)
        if not issue_path.is_absolute():
            issue_path = PROJECT_ROOT / issue_path
        if not issue_path.exists():
            raise FileNotFoundError(f"issue file not found: {issue_path}")
        return issue_path.read_text(encoding="utf-8").strip()

    return str(args.issue).strip()


async def run() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    load_dotenv(PROJECT_ROOT / ".env")

    issue_text = load_issue_text(args)
    if not issue_text:
        raise ValueError("no issue context provided. pass an issue string or --issue-file.")

    orchestrator = PokestratorOrchestrator()
    name_hint = args.name_hint.strip() or orchestrator._build_new_subagent_name(issue_text)

    logger.info("starting orchestrator spec generation name_hint=%s", name_hint)
    logger.info(
        "analysis settings timeout_seconds=%s permission_mode=%s tools_preset=%s",
        orchestrator.timeout_seconds,
        orchestrator.permission_mode,
        orchestrator.tools_preset,
    )

    try:
        name, description, system_prompt = await orchestrator._build_generated_subagent_spec(
            name_hint=name_hint,
            task_description=issue_text,
        )
    except Exception:
        logger.exception("orchestrator spec generation failed")
        return 1

    payload = {
        "name": name,
        "description": description,
        "system_prompt": system_prompt,
        "name_hint": name_hint,
    }

    if args.save:
        try:
            await init_db()
            saved = await insert_subagent(
                payload["name"],
                payload["description"],
                payload["system_prompt"],
            )
            payload["saved"] = True
            payload["saved_id"] = saved.id
            logger.info("saved generated subagent to db name=%s id=%s", saved.name, saved.id)
        except Exception:
            logger.exception("failed to save generated subagent to db")
            return 1

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print("=== generated_subagent_spec ===")
    print(f"name: {payload['name']}")
    print(f"description: {payload['description']}")
    print("\nsystem_prompt:")
    print(payload["system_prompt"])
    print("\n---")
    print(f"name_hint_used: {payload['name_hint']}")
    if args.save:
        print(f"saved_to_db: {payload.get('saved', False)}")
        print(f"saved_id: {payload.get('saved_id', '')}")
    print("=== end_generated_subagent_spec ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
