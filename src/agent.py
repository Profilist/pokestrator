from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from db import Subagent, get_all_subagents, get_subagent_by_name, init_db, insert_subagent
from poke import send_poke_message

try:
    import claude_agent_sdk as claude_sdk
except Exception:
    claude_sdk = None

logger = logging.getLogger("pokestrator.agent")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
RETRY_MESSAGE = (
    "I noticed I don't have the capability to handle this yet. "
    "I have just built and saved a new subagent to handle this. "
    "Please ask me your question again."
)
STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "my",
    "of",
    "on",
    "the",
    "to",
    "with",
    "me",
    "you",
    "your",
}


class PokestratorOrchestrator:
    def __init__(self):
        self.timeout_seconds = int(os.getenv("POKESTRATOR_AGENT_TIMEOUT", "90"))

    async def orchestrate(
        self, request_id: str, task_description: str, metadata: str | None = None
    ) -> str:
        try:
            await init_db()
        except Exception:
            logger.exception("database init failed; using fallback-only execution path")

        try:
            metadata_dict = self._parse_metadata(metadata)
            try:
                decision = await self._decide_route(task_description)
            except Exception as exc:
                logger.exception("orchestrator decision failed")
                raise RuntimeError(f"Could not determine route: {exc}") from exc

            result = await self._execute_decision(task_description, decision, request_id)

            payload = {
                "request_id": request_id,
                "status": "completed",
                "task_description": task_description,
                "branch": decision["branch"],
                "result": result,
            }
            if metadata_dict:
                payload["metadata"] = metadata_dict

            callback_result = await asyncio.to_thread(
                send_poke_message, self._format_poke_message(result, request_id), payload
            )
            logger.info(
                "poke callback sent request_id=%s status=%s",
                request_id,
                callback_result.get("status_code", "dry_run"),
            )
            return result
        except Exception as exc:
            err = f"Error while processing request {request_id}: {exc}"
            logger.exception(err)
            payload = {
                "request_id": request_id,
                "status": "failed",
                "task_description": task_description,
                "error": str(exc),
            }
            try:
                await asyncio.to_thread(
                    send_poke_message, self._format_poke_message(err, request_id), payload
                )
            except Exception:
                logger.exception("failed to post failure message to poke")
            return err

    async def _decide_route(self, task_description: str) -> dict[str, Any]:
        task = task_description.lower()

        try:
            subagents = await get_all_subagents()
        except Exception:
            logger.exception("failed to load subagents, continuing with empty list")
            subagents = []

        match = self._match_existing_subagent(task, subagents)
        if match:
            return {"branch": "match", "subagent": match}

        template_name = self._match_template(task)
        if template_name:
            return {"branch": "template", "template_name": template_name}

        return {"branch": "build_new", "new_subagent_name": self._build_new_subagent_name(task_description)}

    async def _execute_decision(
        self, task_description: str, decision: dict[str, Any], request_id: str
    ) -> str:
        branch = decision["branch"]
        if branch == "match":
            subagent = decision["subagent"]
            return await self._run_subagent(subagent, task_description, request_id)

        if branch == "template":
            template_name = decision["template_name"]
            template_subagent = await self._load_template(template_name)
            if template_subagent is None:
                raise RuntimeError(f"template '{template_name}' is missing or invalid")
            return await self._run_subagent(template_subagent, task_description, request_id)

        _ = decision["new_subagent_name"]
        await self._store_generated_subagent(decision["new_subagent_name"], task_description)
        return RETRY_MESSAGE

    async def _run_subagent(
        self, subagent: Subagent, task_description: str, request_id: str
    ) -> str:
        if claude_sdk is None:
            logger.info(
                "claude sdk unavailable; using deterministic fallback for request_id=%s",
                request_id,
            )
            return self._simulate_subagent_response(subagent.name, task_description)

        return await self._run_claude_agent(subagent, task_description)

    async def _load_template(self, template_name: str) -> Subagent | None:
        template_path = TEMPLATES_DIR / f"{template_name}.json"
        if not template_path.exists():
            return None

        try:
            data = json.loads(template_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.exception("failed to load template=%s", template_name)
            raise ValueError(f"template '{template_name}' could not be parsed: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(f"template '{template_name}' must be a JSON object")

        for field in ("name", "description", "system_prompt"):
            if field not in data or not isinstance(data[field], str) or not data[field].strip():
                raise ValueError(f"template '{template_name}' missing required field '{field}'")

        candidate = Subagent(
            id="",
            name=data["name"].strip(),
            description=data["description"].strip(),
            system_prompt=data["system_prompt"].strip(),
        )

        try:
            existing = await get_subagent_by_name(candidate.name)
            if existing:
                return existing

            return await insert_subagent(
                candidate.name,
                candidate.description,
                candidate.system_prompt,
            )
        except Exception:
            logger.exception("failed to persist template='%s' to DB; using in-memory copy", template_name)
            return candidate

    async def _store_generated_subagent(self, name_hint: str, task_description: str) -> None:
        generated = Subagent(
            id="",
            name=name_hint,
            description=(
                f"Auto-generated agent for: {task_description[:180]}"
                if task_description
                else "Auto-generated subagent"
            ),
            system_prompt=(
                "You are an AI coding and support assistant that solves tasks exactly "
                f"as requested: {task_description[:500]}."
            ),
        )

        try:
            existing = await get_subagent_by_name(generated.name)
            if existing:
                return
        except Exception:
            logger.exception("could not check existing subagent before insert: name=%s", generated.name)
            return

        try:
            await insert_subagent(
                generated.name,
                generated.description,
                generated.system_prompt,
            )
            logger.info("stored generated subagent name=%s", generated.name)
        except Exception:
            logger.exception("failed to store generated subagent name=%s", generated.name)

    async def _run_claude_agent(self, subagent: Subagent, task_description: str) -> str:
        options = claude_sdk.ClaudeAgentOptions(
            system_prompt=subagent.system_prompt,
            allowed_tools=["Bash", "Read", "Write"],
            permission_mode="acceptEdits",
        )

        try:
            stream = claude_sdk.query(prompt=task_description, options=options)
        except Exception as exc:
            logger.exception("failed to initialize claude query")
            return self._simulate_subagent_response(
                subagent.name, f"{task_description} ({exc})"
            )

        if asyncio.iscoroutine(stream):
            stream = await stream

        async def consume() -> str:
            final_result: str | None = None
            chunks: list[str] = []

            async for event in stream:
                candidate = self._extract_text(event)
                if candidate:
                    chunks.append(candidate)
                event_result = self._extract_result(event)
                if event_result:
                    final_result = event_result

            if final_result:
                return final_result.strip()
            if chunks:
                return "\n".join(chunks).strip()
            return self._simulate_subagent_response(subagent.name, task_description)

        try:
            return await asyncio.wait_for(consume(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            return self._simulate_subagent_response(
                subagent.name,
                f"{task_description} (timed out waiting for Claude SDK)",
            )

    def _match_template(self, task: str) -> str | None:
        if "stripe" in task and ("income" in task or "revenue" in task):
            return "stripe_analyst"
        if "use a template" in task and "stripe" in task:
            return "stripe_analyst"
        return None

    def _match_existing_subagent(self, task: str, subagents: list[Subagent]) -> Subagent | None:
        task_tokens = self._tokenize(task)
        if not task_tokens:
            return None

        winner = None
        winner_score = 0

        for subagent in subagents:
            score = 0
            name_text = f" {subagent.name.lower()} "
            description_text = f" {subagent.description.lower()} "
            for token in task_tokens:
                if token in name_text:
                    score += 3
                if token in description_text:
                    score += 1
            if score > winner_score:
                winner = subagent
                winner_score = score

        return winner if winner_score >= 2 else None

    def _build_new_subagent_name(self, task_description: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", task_description.lower().strip())
        slug = slug.strip("_")
        if len(slug) < 6:
            slug = "generated_subagent"
        if len(slug) > 56:
            slug = slug[:56].strip("_")
        return f"auto_{slug[:56]}"

    def _extract_text(self, event: Any) -> str:
        if event is None:
            return ""

        if isinstance(event, str):
            return event.strip()

        if isinstance(event, dict):
            chunks = []
            for key in ("content", "text", "message", "result"):
                value = event.get(key)
                if isinstance(value, str):
                    chunks.append(value.strip())
                elif isinstance(value, list):
                    nested = []
                    for item in value:
                        extracted = self._extract_text(item)
                        if extracted:
                            nested.append(extracted)
                    chunks.append("\n".join(nested).strip())
                elif value is None:
                    continue
            return "\n".join(filter(None, chunks)).strip()

        for attr in ("text", "message", "content", "result"):
            value = getattr(event, attr, None)
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, list):
                nested = [self._extract_text(item) for item in value]
                text = "\n".join(filter(None, nested)).strip()
                if text:
                    return text

        return ""

    def _extract_result(self, event: Any) -> str | None:
        if isinstance(event, dict):
            value = event.get("result")
            return value if isinstance(value, str) and value.strip() else None

        value = getattr(event, "result", None)
        return value if isinstance(value, str) and value.strip() else None

    def _simulate_subagent_response(self, subagent_name: str, task_description: str) -> str:
        return (
            f"[Fallback {subagent_name}] Received: {task_description}. "
            "I don't have direct Claude SDK execution in this environment."
        )

    def _format_poke_message(self, result: str, request_id: str) -> str:
        return f"[pokestrator:{request_id}] {result}"

    def _parse_metadata(self, metadata: str | None) -> dict[str, Any] | None:
        if not metadata:
            return None
        try:
            data = json.loads(metadata)
            return data if isinstance(data, dict) else None
        except Exception:
            logger.warning("invalid metadata json provided")
            return None

    def _tokenize(self, text: str) -> set[str]:
        raw_tokens = re.findall(r"[a-z0-9]+", text.lower())
        return {token for token in raw_tokens if len(token) > 2 and token not in STOP_WORDS}
