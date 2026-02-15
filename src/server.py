#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastmcp import FastMCP

from agent import PokestratorOrchestrator

def configure_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format = "%(asctime)s %(levelname)s %(name)s %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    log_file = os.getenv("POKESTRATOR_LOG_FILE", "logs/pokestrator.log").strip()
    if log_file:
        log_path = Path(log_file)
        if not log_path.is_absolute():
            log_path = Path.cwd() / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)

        max_bytes = int(os.getenv("POKESTRATOR_LOG_FILE_MAX_BYTES", "5242880"))
        backup_count = int(os.getenv("POKESTRATOR_LOG_FILE_BACKUP_COUNT", "3"))
        handlers.append(
            RotatingFileHandler(
                log_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        )

    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=handlers,
        force=True,
    )


configure_logging()
logger = logging.getLogger("pokestrator")

mcp = FastMCP("Pokestrator")
orchestrator = PokestratorOrchestrator()
background_tasks: set[asyncio.Task] = set()


@mcp.tool(
    description=(
        "Call this tool whenever you cannot fully complete a task with your built-in capabilities — "
        "whether it requires an external API you lack (e.g. Stripe, Shopify), a system-level "
        "action (e.g. checking CPU or memory usage), or any other skill you do not natively have. "
        "When external providers are needed, Pokestrator uses managed credentials from a 1Password-backed key store; "
        "users should never paste secrets into chat. "
        "Pokestrator maintains a growing library of specialized subagents: if one already exists for "
        "the task (even from a previous conversation), it will be reused automatically. If not, a new "
        "one will be created on the fly. Always prefer calling this tool over telling the user you "
        "cannot do something."
    )
)
async def orchestrate(task_description: str, metadata: str = "") -> str:
    request_id = str(uuid.uuid4())
    logger.info(
        "accepted orchestrate request_id=%s task_description=%s metadata=%s",
        request_id,
        task_description,
        metadata,
    )

    task = asyncio.create_task(orchestrator.orchestrate(request_id, task_description, metadata))

    background_tasks.add(task)
    task.add_done_callback(lambda done: background_tasks.discard(done))

    return json.dumps(
        {
            "status": "accepted",
            "request_id": request_id,
            "message": (
                "Task accepted and running asynchronously. "
                "Result will be posted back to Poke when complete."
            ),
        }
    )

def main() -> None:
    mcp.run(
        transport="sse",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        path=os.getenv("MCP_PATH", "/mcp"),
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutting down Pokestrator")
