#!/usr/bin/env python3
from __future__ import annotations

import logging
import os

from fastmcp import FastMCP

from agent import PokestratorOrchestrator

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("pokestrator")

mcp = FastMCP("Pokestrator")
orchestrator = PokestratorOrchestrator()


@mcp.tool(description="Master Pokestrator tool for Poke limitations: call this when a task exceeds current capabilities; it will reuse or create specialized subagents and return a completed result.")
async def orchestrate(task_description: str) -> str:
    logger.info("orchestrate.test_response task=%s", task_description)
    return "This is a test response from the Pokestrator MCP server"
    # return await orchestrator.orchestrate(task_description)


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    # Poke spec expects /sse endpoint for MCP integration.
    mcp_path = os.getenv("MCP_PATH", "/sse")

    logger.info("Starting FastMCP server host=%s port=%s path=%s", host, port, mcp_path)
    mcp.run(
        transport="sse",
        host=host,
        port=port,
        path=mcp_path,
    )
