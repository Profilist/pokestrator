from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

logger = logging.getLogger("pokestrator.agent")


@dataclass(slots=True)
class SubagentSpec:
    id: str
    name: str
    description: str
    prompt_md: str
    connectors: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


class SubagentStore:
    """Temporary in-memory store.

    TODO: Replace with Render PostgreSQL-backed persistence.
    """

    def __init__(self) -> None:
        self._specs: list[SubagentSpec] = []

    async def find_for_task(self, task_description: str) -> SubagentSpec | None:
        words = _normalize_words(task_description)
        best_match: SubagentSpec | None = None
        best_score = 0

        for spec in self._specs:
            spec_words = _normalize_words(f"{spec.name} {spec.description}")
            score = len(words.intersection(spec_words))
            if score > best_score:
                best_match = spec
                best_score = score

        return best_match

    async def save(self, spec: SubagentSpec) -> None:
        self._specs.append(spec)

    async def increment_usage(self, spec_id: str) -> None:
        for spec in self._specs:
            if spec.id == spec_id:
                spec.metrics["uses"] = int(spec.metrics.get("uses", 0)) + 1
                spec.metrics["last_used"] = datetime.now(timezone.utc).isoformat()
                return


class SubagentCreator:
    """Creates subagent specs from task limitations.

    TODO: Generate stronger specs using an LLM + capability discovery.
    """

    async def create(self, task_description: str) -> SubagentSpec:
        slug = _slugify(task_description)[:40] or "general_analyst"
        prompt_md = (
            "You are a specialized subagent created by Pokestrator.\n"
            "Solve the assigned task with a direct, evidence-based answer.\n"
            "If external integrations are needed and unavailable, state exactly what is missing.\n"
            f"Specialization target: {task_description.strip()}"
        )
        return SubagentSpec(
            id=str(uuid.uuid4()),
            name=slug,
            description=f"Specialized agent for: {task_description.strip()}",
            prompt_md=prompt_md,
            connectors={},
            metrics={"uses": 0},
        )


class SubagentRunner:
    """Runs a subagent spec through Claude Agent SDK integration."""

    async def run(self, spec: SubagentSpec, task_description: str) -> str:
        runtime_prompt = (
            f"SYSTEM PLAYBOOK:\n{spec.prompt_md}\n\n"
            f"TASK:\n{task_description.strip()}\n\n"
            "Return a concise, actionable result."
        )
        return await run_agent(runtime_prompt)


class PokestratorOrchestrator:
    """Router + lifecycle manager for subagents."""

    def __init__(
        self,
        store: SubagentStore | None = None,
        creator: SubagentCreator | None = None,
        runner: SubagentRunner | None = None,
    ) -> None:
        self._store = store or SubagentStore()
        self._creator = creator or SubagentCreator()
        self._runner = runner or SubagentRunner()

    async def orchestrate(self, task_description: str) -> str:
        task_description = task_description.strip()
        if not task_description:
            raise ValueError("task_description cannot be empty")

        logger.info("orchestrate.start task=%s", task_description)

        spec = await self._store.find_for_task(task_description)
        created = False

        if spec is None:
            spec = await self._creator.create(task_description)
            await self._store.save(spec)
            created = True
            logger.info("orchestrate.subagent_created id=%s name=%s", spec.id, spec.name)
        else:
            logger.info("orchestrate.subagent_reused id=%s name=%s", spec.id, spec.name)

        result = await self._runner.run(spec, task_description)
        await self._store.increment_usage(spec.id)

        # TODO: Post result back to Poke API if webhook/callback contract is configured.

        status = "created" if created else "reused"
        return (
            f"subagent_status={status}; subagent_name={spec.name}; "
            f"subagent_id={spec.id}\n\n{result}"
        )


async def run_agent(prompt: str) -> str:
    """Run a Claude agent prompt and return streamed output as one string."""
    output_parts: list[str] = []

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            allowed_tools=["Read", "Edit", "Glob", "Grep", "Bash"],
            permission_mode="acceptEdits",
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "text"):
                    output_parts.append(block.text)
                elif hasattr(block, "name"):
                    output_parts.append(f"[Tool: {block.name}]")
        elif isinstance(message, ResultMessage):
            output_parts.append(f"[Done: {message.subtype}]")

    return "\n".join(output_parts)


def _normalize_words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9_]+", text.lower()) if len(w) > 2}


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "agent"


if __name__ == "__main__":
    demo_task = "Pull Stripe revenue by product and summarize weekly trend."
    print(asyncio.run(PokestratorOrchestrator().orchestrate(demo_task)))
