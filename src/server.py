#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid

from fastmcp import FastMCP

from agent import PokestratorOrchestrator

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("pokestrator")

mcp = FastMCP("Pokestrator")
orchestrator = PokestratorOrchestrator()
background_tasks: set[asyncio.Task] = set()


@mcp.tool(
    description=(
        "Master Pokestrator tool for Poke limitations: call this when a task exceeds current "
        "capabilities; it will reuse or create specialized subagents and delegate asynchronously."
    )
)
async def orchestrate(task_description: str, metadata: str = "") -> str:
    request_id = str(uuid.uuid4())
    logger.info("accepted orchestrate request_id=%s", request_id)

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
