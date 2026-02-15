from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from db import (
    Subagent,
    get_all_subagents,
    get_api_key,
    get_subagent_by_name,
    init_db,
    insert_subagent,
    update_subagent_auth,
)
from poke import send_poke_message
from routing import SubagentRouter
import claude_agent_sdk as claude_sdk

logger = logging.getLogger("pokestrator.agent")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

PROVIDER_HINTS: dict[str, tuple[str, ...]] = {
    "google_search_console": (
        "google search console",
        "search console",
        "gsc",
        "google search",
        "search impressions",
    ),
    "google_analytics": ("google analytics", "ga4"),
    "stripe": ("stripe",),
    "shopify": ("shopify",),
    "hubspot": ("hubspot",),
    "salesforce": ("salesforce",),
    "github": ("github",),
    "twilio": (
        "twilio",
        "sms",
        "text message",
        "send sms",
        "send text",
        "mms",
    ),
}

PROVIDER_API_PROFILES: dict[str, dict[str, str]] = {
    "google_search_console": {
        "api_name": "Google Search Console API",
        "docs_url": "https://developers.google.com/webmaster-tools",
        "base_url": "https://www.googleapis.com/webmasters/v3",
    },
    "google_analytics": {
        "api_name": "Google Analytics Data API (GA4)",
        "docs_url": "https://developers.google.com/analytics/devguides/reporting/data/v1",
        "base_url": "https://analyticsdata.googleapis.com/v1beta",
    },
    "stripe": {
        "api_name": "Stripe API",
        "docs_url": "https://docs.stripe.com/api",
        "base_url": "https://api.stripe.com/v1",
    },
    "shopify": {
        "api_name": "Shopify Admin API",
        "docs_url": "https://shopify.dev/docs/api",
        "base_url": "https://{shop}.myshopify.com/admin/api",
    },
    "hubspot": {
        "api_name": "HubSpot APIs",
        "docs_url": "https://developers.hubspot.com/docs/api/overview",
        "base_url": "https://api.hubapi.com",
    },
    "salesforce": {
        "api_name": "Salesforce REST API",
        "docs_url": "https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/",
        "base_url": "https://{instance}.salesforce.com/services/data",
    },
    "github": {
        "api_name": "GitHub REST API",
        "docs_url": "https://docs.github.com/en/rest",
        "base_url": "https://api.github.com",
    },
    "twilio": {
        "api_name": "Twilio Programmable Messaging API",
        "docs_url": "https://www.twilio.com/docs/messaging/api/message-resource",
        "base_url": "https://api.twilio.com/2010-04-01/Accounts/ACe294c06a1bd961e962bede79cec03398",
    },
}


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

            result, execution_status, execution_metadata = await self._execute_decision(
                task_description,
                decision,
                request_id,
            )

            payload = {
                "request_id": request_id,
                "status": execution_status,
                "task_description": task_description,
                "branch": decision["branch"],
                "result": result,
            }
            if execution_metadata:
                payload.update(execution_metadata)
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
    ) -> tuple[str, str, dict[str, Any]]:
        branch = decision["branch"]
        if branch == "match":
            subagent = decision["subagent"]
            return await self._run_subagent_with_auth_check(
                subagent=subagent,
                task_description=task_description,
                request_id=request_id,
                branch=branch,
            )

        required_provider = self._infer_required_provider(task_description)
        subagent = await self._store_generated_subagent(
            decision["new_subagent_name"],
            task_description,
            required_provider=required_provider,
        )
        logger.info(
            "orchestrator route=build_new prepared subagent=%s request_id=%s",
            subagent.name,
            request_id,
        )
        return await self._run_subagent_with_auth_check(
            subagent=subagent,
            task_description=task_description,
            request_id=request_id,
            branch=branch,
        )

    async def _run_subagent_with_auth_check(
        self,
        subagent: Subagent,
        task_description: str,
        request_id: str,
        branch: str,
    ) -> tuple[str, str, dict[str, Any]]:
        required_provider = subagent.required_provider
        if not required_provider:
            inferred_provider = self._infer_required_provider(
                f"{task_description}\n{subagent.name}\n{subagent.description}"
            )
            if inferred_provider:
                required_provider = inferred_provider
                subagent.required_provider = inferred_provider

        if required_provider:
            api_key = await get_api_key(required_provider)
            if not api_key:
                await self._set_subagent_auth_status(subagent, status="needs_api_key")
                message = self._build_missing_managed_api_key_message(
                    provider=required_provider,
                    subagent_name=subagent.name,
                )
                return (
                    message,
                    "missing_managed_api_key",
                    {
                        "required_provider": required_provider,
                        "subagent_name": subagent.name,
                        "credential_source": "1password",
                    },
                )

            await self._set_subagent_auth_status(subagent, status="ready")
            await self._send_progress_callback(
                request_id=request_id,
                task_description=task_description,
                branch=branch,
                message=self._build_managed_api_key_found_message(required_provider),
                metadata={
                    "subagent_name": subagent.name,
                    "required_provider": required_provider,
                    "credential_source": "1password",
                },
            )
            result = await self._run_subagent(
                subagent,
                task_description,
                request_id,
                api_key=api_key,
            )
            return (
                result,
                "completed",
                {
                    "required_provider": required_provider,
                    "credential_source": "1password",
                },
            )

        result = await self._run_subagent(subagent, task_description, request_id)
        return result, "completed", {}

    async def _run_subagent(
        self,
        subagent: Subagent,
        task_description: str,
        request_id: str,
        api_key: str | None = None,
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

        return await self._run_claude_agent(
            subagent=subagent,
            task_description=task_description,
            request_id=request_id,
            api_key=api_key,
        )

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

    async def _store_generated_subagent(
        self,
        name_hint: str,
        task_description: str,
        required_provider: str | None = None,
    ) -> Subagent:
        name, description, system_prompt = await self._build_generated_subagent_spec(
            name_hint=name_hint,
            task_description=task_description,
        )
        normalized_provider = self._normalize_provider(required_provider)
        if normalized_provider:
            system_prompt = (
                f"{system_prompt}\n\n"
                f"{self._build_provider_capability_instructions(normalized_provider)}"
            )
        provider_key = await get_api_key(normalized_provider) if normalized_provider else None
        subagent_status = "needs_api_key" if normalized_provider and not provider_key else "ready"
        generated = Subagent(
            id="",
            name=name,
            description=description,
            system_prompt=system_prompt,
            status=subagent_status,
            required_provider=normalized_provider,
        )

        try:
            existing = await get_subagent_by_name(generated.name)
            if existing:
                updated = await update_subagent_auth(
                    existing.id,
                    status=subagent_status,
                    required_provider=normalized_provider or existing.required_provider,
                )
                if updated:
                    logger.info(
                        "reusing existing generated subagent name=%s status=%s required_provider=%s",
                        updated.name,
                        updated.status,
                        updated.required_provider or "",
                    )
                    return updated
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
                status=subagent_status,
                required_provider=normalized_provider,
            )
            logger.info(
                "stored generated subagent name=%s status=%s required_provider=%s",
                generated.name,
                subagent_status,
                normalized_provider or "",
            )
            return stored
        except Exception:
            logger.exception("failed to store generated subagent name=%s", generated.name)
            return generated

    async def _run_claude_agent(
        self,
        subagent: Subagent,
        task_description: str,
        request_id: str,
        api_key: str | None = None,
    ) -> str:
        logger.info(
            "starting claude run request_id=%s subagent=%s timeout_seconds=%s permission_mode=%s tools_preset=%s",
            request_id,
            subagent.name,
            self.timeout_seconds,
            self.permission_mode,
            self.tools_preset,
        )
        system_prompt = subagent.system_prompt
        if api_key and subagent.required_provider:
            system_prompt = (
                f"{system_prompt}\n\n"
                "Provider authentication context (supplied by orchestrator):\n"
                f"- provider: {subagent.required_provider}\n"
                "- api_key_source: retrieved by orchestrator from 1Password-managed DB credentials\n"
                f"- api_key: {api_key}\n"
                f"{self._build_provider_runtime_auth_instructions(subagent.required_provider)}"
            )
        options = claude_sdk.ClaudeAgentOptions(
            system_prompt=system_prompt,
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

    def _infer_required_provider(self, task_description: str) -> str | None:
        task = task_description.lower()
        for provider, hints in PROVIDER_HINTS.items():
            for hint in hints:
                if hint in task:
                    return provider
        return None

    def _build_provider_capability_instructions(self, provider: str) -> str:
        profile = PROVIDER_API_PROFILES.get(provider)
        if profile:
            base = (
                "Provider/API requirements for this capability:\n"
                f"- required_provider: {provider}\n"
                f"- use API: {profile['api_name']}\n"
                f"- docs: {profile['docs_url']}\n"
                f"- base_url: {profile['base_url']}\n"
                "- API key source: Pokestrator retrieves a 1Password-managed key from DB and injects it at runtime.\n"
                "- Do not ask the user for credentials when api_key is already present in runtime context.\n"
                "- If mentioning credentials, explicitly state they were loaded from 1Password-managed storage.\n"
                f"- {self._build_provider_runtime_auth_instructions(provider)}"
            )
        else:
            base = (
                "Provider/API requirements for this capability:\n"
                f"- required_provider: {provider}\n"
                "- API key source: Pokestrator retrieves a 1Password-managed key from DB and injects it at runtime.\n"
                "- Use this runtime credential for authenticated calls and follow provider docs.\n"
                "- Do not ask the user for credentials when api_key is already present in runtime context.\n"
                "- If mentioning credentials, explicitly state they were loaded from 1Password-managed storage."
            )

        if provider == "twilio":
            base = (
                f"{base}\n"
                "- For SMS send operations, use this exact request pattern in Bash/curl (the other fields are fixed for this project setup):\n"
                f"  curl -X POST \"https://api.twilio.com/2010-04-01/Accounts/ACe294c06a1bd961e962bede79cec03398/Messages.json\" \\\n"
                f"    -u \"SK16b3fe30b27a26c49cbbc1cea05859f2:<api_key_from_runtime_context>\" \\\n"
                "    --data-urlencode \"To=<phone_number_from_task_context>\" \\\n"
                f"    --data-urlencode \"From=+17753678319\" \\\n"
                "    --data-urlencode \"Body=<message_body_from_task_context>\"\n"
                "- Keep Account SID, API Key SID, and From number fixed for this project setup.\n"
                "- Only vary To and Body from task context, and api_key from orchestrator runtime context.\n"
            )

        return base

    def _build_provider_runtime_auth_instructions(self, provider: str) -> str:
        if provider == "twilio":
            return (
                f"Twilio auth details: api_key is the API key secret (password). Use HTTP Basic Auth with "
                f"username 'SK16b3fe30b27a26c49cbbc1cea05859f2' and password '<api_key>' against account "
                f"'ACe294c06a1bd961e962bede79cec03398'."
            )
        return (
            "Use this key for authenticated API calls. Prefer Authorization: Bearer <api_key> unless "
            "provider documentation clearly specifies a different header or query format."
        )

    def _build_managed_api_key_found_message(self, provider: str) -> str:
        return (
            f"Tell the user explicitly that Pokestrator found their {provider} API key from 1Password and is pulling their data now."
        )

    async def _set_subagent_auth_status(self, subagent: Subagent, status: str) -> None:
        if not subagent.id:
            return
        try:
            updated = await update_subagent_auth(
                subagent.id,
                status=status,
                required_provider=subagent.required_provider,
            )
            if updated:
                subagent.status = updated.status
                subagent.required_provider = updated.required_provider
        except Exception:
            logger.exception(
                "failed to update subagent auth status subagent=%s status=%s",
                subagent.name,
                status,
            )

    def _build_missing_managed_api_key_message(self, provider: str, subagent_name: str) -> str:
        return (
            f"Tell the user that Pokestrator couldn't find an API key for {provider}, please upload it to 1Password "
            "and we'll be able to handle that."
        )

    def _normalize_provider(self, value: str | None) -> str | None:
        if not value:
            return None
        normalized = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower()).strip("_")
        return normalized or None

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
            "- Prefer capability categories that cover multiple related tasks using the same "
            "execution strategy (same runtime, commands, tools, or API family).\n"
            "- If neighboring requests can be handled with the same shell command family or API "
            "calls, create one broader capability instead of a metric-specific one.\n"
            "- Example: RAM usage request -> auto_system_resources_check (broader), not "
            "auto_system_memory_check (too narrow).\n"
            "- Do not hardcode a single person/company/date.\n"
            "- Avoid request-instance words in names such as current, today, one_user, or a "
            "single metric when a category name fits.\n"
            "- If the task requires an external integration, pick a concrete API/provider by name "
            "in system_prompt (not vague wording).\n"
            "- Explicitly include the API name and a docs URL in system_prompt when a provider is needed.\n"
            "- Example: SMS sending tasks should specify Twilio Programmable Messaging API and "
            "mention the Message Resource endpoint.\n"
            "- Keep name concise, snake_case, and prefixed with auto_.\n"
            "- Description should describe the capability class and reusable scope, not one request.\n"
            "- system_prompt should tell the subagent to choose exact commands or API operations "
            "dynamically per task inside that category.\n"
        )

        options = claude_sdk.ClaudeAgentOptions(
            system_prompt=(
                "You generate reusable subagent specifications for an orchestrator system. "
                "Bias toward category-level capabilities with broad reuse when tasks share the "
                "same execution strategy. "
                "When external services are required, force concrete provider/API selection instead of "
                "generic 'figure it out' language. "
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

    async def _send_progress_callback(
        self,
        *,
        request_id: str,
        task_description: str,
        branch: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "request_id": request_id,
            "status": "in_progress",
            "task_description": task_description,
            "branch": branch,
            "result": message,
        }
        if metadata:
            payload.update(metadata)

        callback_message = self._format_poke_message(message, request_id)
        try:
            logger.info(
                "poke progress callback outgoing request_id=%s message=%s payload=%s",
                request_id,
                callback_message,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            )
            callback_result = await asyncio.to_thread(
                send_poke_message,
                callback_message,
                payload,
            )
            logger.info(
                "poke progress callback sent request_id=%s status=%s response=%s",
                request_id,
                callback_result.get("status_code", "dry_run"),
                callback_result.get("response", ""),
            )
        except Exception:
            logger.exception(
                "failed to send poke progress callback request_id=%s",
                request_id,
            )

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
