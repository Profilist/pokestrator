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
from routing import SubagentRouter
import claude_agent_sdk as claude_sdk

logger = logging.getLogger("pokestrator.agent")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
class PokestratorOrchestrator:
    def __init__(self):
        self.timeout_seconds = int(os.getenv("POKESTRATOR_AGENT_TIMEOUT", "180"))
        self.permission_mode = os.getenv("POKESTRATOR_PERMISSION_MODE", "bypassPermissions")
        self.tools_preset = os.getenv("POKESTRATOR_TOOLS_PRESET", "claude_code")
        self.route_min_score = max(1, int(os.getenv("POKESTRATOR_ROUTE_MIN_SCORE", "2")))
        self.route_confident_score = max(
            self.route_min_score,
            int(os.getenv("POKESTRATOR_ROUTE_CONFIDENT_SCORE", "7")),
        )
        self.route_confident_margin = max(
            1,
            int(os.getenv("POKESTRATOR_ROUTE_CONFIDENT_MARGIN", "4")),
        )
        self.route_llm_enabled = os.getenv("POKESTRATOR_ROUTE_LLM_ENABLED", "1") == "1"
        self.route_llm_top_k = max(1, int(os.getenv("POKESTRATOR_ROUTE_LLM_TOP_K", "3")))
        self.route_llm_timeout_seconds = max(
            3,
            int(os.getenv("POKESTRATOR_ROUTE_LLM_TIMEOUT_SECONDS", "12")),
        )
        self.route_llm_min_confidence = min(
            1.0,
            max(0.0, float(os.getenv("POKESTRATOR_ROUTE_LLM_MIN_CONFIDENCE", "0.6"))),
        )
        self.log_agent_events = os.getenv("POKESTRATOR_LOG_AGENT_EVENTS", "1") == "1"
        self.event_text_preview_len = int(
            os.getenv("POKESTRATOR_AGENT_EVENT_TEXT_PREVIEW_LEN", "260")
        )
        self.router = SubagentRouter(
            permission_mode=self.permission_mode,
            timeout_seconds=self.timeout_seconds,
            route_min_score=self.route_min_score,
            route_confident_score=self.route_confident_score,
            route_confident_margin=self.route_confident_margin,
            route_llm_enabled=self.route_llm_enabled,
            route_llm_top_k=self.route_llm_top_k,
            route_llm_timeout_seconds=self.route_llm_timeout_seconds,
            route_llm_min_confidence=self.route_llm_min_confidence,
            collect_response_text=self._collect_response_text,
            parse_json_object=self._parse_json_object,
            normalize_text_field=self._normalize_text_field,
            preview_text=self._preview,
        )

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

            callback_message = self._format_poke_message(result, request_id)
            logger.info(
                "poke callback outgoing request_id=%s message=%s payload=%s",
                request_id,
                callback_message,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            )
            callback_result = await asyncio.to_thread(
                send_poke_message, callback_message, payload
            )
            logger.info(
                "poke callback sent request_id=%s status=%s response=%s",
                request_id,
                callback_result.get("status_code", "dry_run"),
                callback_result.get("response", ""),
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
                callback_message = self._format_poke_message(err, request_id)
                logger.info(
                    "poke callback outgoing request_id=%s message=%s payload=%s",
                    request_id,
                    callback_message,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                )
                await asyncio.to_thread(
                    send_poke_message, callback_message, payload
                )
            except Exception:
                logger.exception("failed to post failure message to poke")
            return err

    async def _decide_route(self, task_description: str) -> dict[str, Any]:
        try:
            subagents = await get_all_subagents()
        except Exception:
            logger.exception("failed to load subagents, continuing with empty list")
            subagents = []

        return await self.router.decide_route(
            task_description=task_description,
            subagents=subagents,
            build_new_subagent_name=self._build_new_subagent_name,
        )

    async def _execute_decision(
        self, task_description: str, decision: dict[str, Any], request_id: str
    ) -> str:
        branch = decision["branch"]
        if branch == "match":
            subagent = decision["subagent"]
            return await self._run_subagent(subagent, task_description, request_id)

        subagent = await self._store_generated_subagent(
            decision["new_subagent_name"],
            task_description,
        )
        logger.info(
            "orchestrator route=build_new running newly available subagent=%s request_id=%s",
            subagent.name,
            request_id,
        )
        return await self._run_subagent(subagent, task_description, request_id)

    async def _run_subagent(
        self, subagent: Subagent, task_description: str, request_id: str
    ) -> str:
        logger.info(
            "running subagent request_id=%s subagent=%s description=%s",
            request_id,
            subagent.name,
            self._preview(subagent.description),
        )
        if claude_sdk is None:
            logger.info(
                "claude sdk import unavailable; using fallback for request_id=%s",
                request_id,
            )
            return self._simulate_subagent_response(
                subagent_name=subagent.name,
                task_description=task_description,
                reason=(
                    "claude_agent_sdk is not available in this runtime "
                    "(import failed or package missing)"
                ),
            )

        return await self._run_claude_agent(subagent, task_description, request_id)

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

    async def _store_generated_subagent(self, name_hint: str, task_description: str) -> Subagent:
        name, description, system_prompt = await self._build_generated_subagent_spec(
            name_hint=name_hint,
            task_description=task_description,
        )
        generated = Subagent(
            id="",
            name=name,
            description=description,
            system_prompt=system_prompt,
        )

        try:
            existing = await get_subagent_by_name(generated.name)
            if existing:
                logger.info("reusing existing generated subagent name=%s", existing.name)
                return existing
        except Exception:
            logger.exception("could not check existing subagent before insert: name=%s", generated.name)
            return generated

        try:
            stored = await insert_subagent(
                generated.name,
                generated.description,
                generated.system_prompt,
            )
            logger.info("stored generated subagent name=%s", generated.name)
            return stored
        except Exception:
            logger.exception("failed to store generated subagent name=%s", generated.name)
            return generated

    async def _run_claude_agent(
        self, subagent: Subagent, task_description: str, request_id: str
    ) -> str:
        logger.info(
            "starting claude run request_id=%s subagent=%s timeout_seconds=%s permission_mode=%s tools_preset=%s",
            request_id,
            subagent.name,
            self.timeout_seconds,
            self.permission_mode,
            self.tools_preset,
        )
        options = claude_sdk.ClaudeAgentOptions(
            system_prompt=subagent.system_prompt,
            tools={"type": "preset", "preset": self.tools_preset},
            permission_mode=self.permission_mode,
        )

        try:
            stream = claude_sdk.query(prompt=task_description, options=options)
        except Exception as exc:
            logger.exception("failed to initialize claude query")
            return self._simulate_subagent_response(
                subagent_name=subagent.name,
                task_description=task_description,
                reason=f"claude query initialization failed: {exc}",
            )

        if asyncio.iscoroutine(stream):
            stream = await stream

        async def consume() -> str:
            final_result: str | None = None
            chunks: list[str] = []
            event_count = 0

            async for event in stream:
                event_count += 1
                if self.log_agent_events:
                    logger.info(
                        "claude event request_id=%s subagent=%s idx=%s type=%s",
                        request_id,
                        subagent.name,
                        event_count,
                        type(event).__name__,
                    )
                    tool_names = self._extract_tool_names(event)
                    if tool_names:
                        logger.info(
                            "claude event tools request_id=%s subagent=%s idx=%s tools=%s",
                            request_id,
                            subagent.name,
                            event_count,
                            ",".join(tool_names),
                        )
                candidate = self._extract_text(event)
                if candidate:
                    chunks.append(candidate)
                    if self.log_agent_events:
                        logger.info(
                            "claude event text request_id=%s subagent=%s idx=%s text=%s",
                            request_id,
                            subagent.name,
                            event_count,
                            self._preview(candidate),
                        )
                event_result = self._extract_result(event)
                if event_result:
                    final_result = event_result
                    logger.info(
                        "claude event result request_id=%s subagent=%s idx=%s text=%s",
                        request_id,
                        subagent.name,
                        event_count,
                        self._preview(event_result),
                    )

            if final_result:
                return final_result.strip()
            if chunks:
                return "\n".join(chunks).strip()
            logger.warning(
                "claude run produced no output request_id=%s subagent=%s",
                request_id,
                subagent.name,
            )
            return self._simulate_subagent_response(
                subagent_name=subagent.name,
                task_description=task_description,
                reason="claude run completed without output",
            )

        try:
            return await asyncio.wait_for(consume(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                "claude run timed out request_id=%s subagent=%s timeout_seconds=%s",
                request_id,
                subagent.name,
                self.timeout_seconds,
            )
            return self._simulate_subagent_response(
                subagent_name=subagent.name,
                task_description=task_description,
                reason=f"claude run timed out after {self.timeout_seconds}s",
            )

    def _match_existing_subagent(self, task: str, subagents: list[Subagent]) -> Subagent | None:
        return self.router.match_existing_subagent(task, subagents)

    def _rank_existing_subagents(
        self, task: str, subagents: list[Subagent]
    ) -> list[dict[str, Any]]:
        return self.router.rank_existing_subagents(task, subagents)

    def _is_confident_ranked_match(self, ranked_matches: list[dict[str, Any]]) -> bool:
        return self.router.is_confident_ranked_match(ranked_matches)

    async def _llm_validate_ranked_match(
        self, task_description: str, ranked_matches: list[dict[str, Any]]
    ) -> Subagent | None:
        return await self.router.llm_validate_ranked_match(task_description, ranked_matches)

    def _build_new_subagent_name(self, task_description: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", task_description.lower()).strip("_")
        if not slug:
            slug = "general_task_automation"
        if len(slug) > 56:
            slug = slug[:56].strip("_")
        return f"auto_{slug}"

    async def _build_generated_subagent_spec(
        self, name_hint: str, task_description: str
    ) -> tuple[str, str, str]:
        analyzed = await self._analyze_task_with_orchestrator(task_description)
        name = self._normalize_subagent_name(
            analyzed.get("name"),
            name_hint or "auto_general_task_automation",
        )
        description = self._normalize_text_field(
            analyzed.get("description"),
            fallback="",
            max_len=240,
        )
        system_prompt = self._normalize_text_field(
            analyzed.get("system_prompt"),
            fallback="",
            max_len=2400,
        )

        if not description or not system_prompt:
            raise RuntimeError(
                "orchestrator capability analysis returned incomplete spec fields"
            )

        logger.info("generated reusable subagent spec name=%s", name)
        return name, description, system_prompt

    async def _analyze_task_with_orchestrator(
        self, task_description: str
    ) -> dict[str, str]:
        if claude_sdk is None:
            raise RuntimeError(
                "cannot analyze capability with orchestrator: claude sdk unavailable"
            )

        analysis_prompt = (
            "Analyze this task and design a reusable subagent spec that can solve future similar "
            "requests, not just this one instance.\n\n"
            f"TASK:\n{task_description}\n\n"
            "Return ONLY a JSON object with exactly these keys:\n"
            '{\n'
            '  "name": "auto_snake_case_name",\n'
            '  "description": "one-sentence reusable capability summary",\n'
            '  "system_prompt": "multi-sentence reusable instructions"\n'
            "}\n\n"
            "Rules:\n"
            "- Generalize across target entities, date ranges, geographies, and filters.\n"
            "- Do not hardcode a single person/company/date.\n"
            "- Keep name concise, snake_case, and prefixed with auto_.\n"
            "- Description should describe the capability class, not one request.\n"
        )

        options = claude_sdk.ClaudeAgentOptions(
            system_prompt=(
                "You generate reusable subagent specifications for an orchestrator system. "
                "Output strict JSON only."
            ),
            allowed_tools=[],
            max_turns=1,
            permission_mode=self.permission_mode,
        )

        try:
            stream = claude_sdk.query(prompt=analysis_prompt, options=options)
            if asyncio.iscoroutine(stream):
                stream = await stream
            response_text = await asyncio.wait_for(
                self._collect_response_text(stream),
                timeout=min(self.timeout_seconds, 45),
            )
        except Exception as exc:
            logger.exception("orchestrator capability analysis failed")
            raise RuntimeError("orchestrator capability analysis failed") from exc

        parsed = self._parse_json_object(response_text)
        if not parsed:
            raise RuntimeError(
                "orchestrator capability analysis did not return valid JSON"
            )

        return {
            "name": str(parsed.get("name", "")).strip(),
            "description": str(parsed.get("description", "")).strip(),
            "system_prompt": str(parsed.get("system_prompt", "")).strip(),
        }

    async def _collect_response_text(self, stream: Any) -> str:
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
        return "\n".join(chunks).strip()

    def _parse_json_object(self, text: str) -> dict[str, Any] | None:
        if not text:
            return None

        candidates: list[str] = [text.strip()]

        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
        if fence_match:
            candidates.append(fence_match.group(1).strip())

        brace_match = re.search(r"(\{.*\})", text, flags=re.DOTALL)
        if brace_match:
            candidates.append(brace_match.group(1).strip())

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue

        return None

    def _normalize_confidence(self, value: Any) -> float:
        return self.router.normalize_confidence(value)

    def _normalize_subagent_name(self, value: Any, fallback: str) -> str:
        raw = str(value or "").strip().lower()
        slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
        if not slug:
            slug = re.sub(r"[^a-z0-9]+", "_", fallback.lower()).strip("_")
        if not slug:
            slug = "auto_general_task_automation"
        if not slug.startswith("auto_"):
            slug = f"auto_{slug}"
        return slug[:64].strip("_")

    def _normalize_text_field(self, value: Any, fallback: str, max_len: int) -> str:
        text = str(value or "").strip()
        if not text:
            return fallback
        text = re.sub(r"\s+", " ", text)
        return text[:max_len].strip()

    def _preview(self, value: str) -> str:
        normalized = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(normalized) <= self.event_text_preview_len:
            return normalized
        return f"{normalized[: self.event_text_preview_len - 3]}..."

    def _extract_tool_names(self, event: Any) -> list[str]:
        names: list[str] = []

        if isinstance(event, dict):
            content = event.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        name = block.get("name")
                        if isinstance(name, str) and name.strip():
                            names.append(name.strip())
            return names

        content = getattr(event, "content", None)
        if isinstance(content, list):
            for block in content:
                name = getattr(block, "name", None)
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
        return names

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

    def _simulate_subagent_response(
        self, subagent_name: str, task_description: str, reason: str
    ) -> str:
        return (
            f"[Fallback {subagent_name}] Unable to complete task. "
            f"Reason: {reason}. "
            f"Task: {task_description}"
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
        return self.router.tokenize(text)
