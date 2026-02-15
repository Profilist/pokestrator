from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable

import claude_agent_sdk as claude_sdk

from db import Subagent

logger = logging.getLogger("pokestrator.routing")

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

CollectResponseText = Callable[[Any], Awaitable[str]]
ParseJsonObject = Callable[[str], dict[str, Any] | None]
NormalizeTextField = Callable[[Any, str, int], str]
PreviewText = Callable[[str], str]
BuildSubagentName = Callable[[str], str]


class SubagentRouter:
    def __init__(
        self,
        *,
        permission_mode: str,
        timeout_seconds: int,
        route_min_score: int,
        route_confident_score: int,
        route_confident_margin: int,
        route_llm_enabled: bool,
        route_llm_top_k: int,
        route_llm_timeout_seconds: int,
        route_llm_min_confidence: float,
        collect_response_text: CollectResponseText,
        parse_json_object: ParseJsonObject,
        normalize_text_field: NormalizeTextField,
        preview_text: PreviewText,
    ) -> None:
        self.permission_mode = permission_mode
        self.timeout_seconds = timeout_seconds
        self.route_min_score = route_min_score
        self.route_confident_score = route_confident_score
        self.route_confident_margin = route_confident_margin
        self.route_llm_enabled = route_llm_enabled
        self.route_llm_top_k = route_llm_top_k
        self.route_llm_timeout_seconds = route_llm_timeout_seconds
        self.route_llm_min_confidence = route_llm_min_confidence
        self.collect_response_text = collect_response_text
        self.parse_json_object = parse_json_object
        self.normalize_text_field = normalize_text_field
        self.preview_text = preview_text

    async def decide_route(
        self,
        task_description: str,
        subagents: list[Subagent],
        build_new_subagent_name: BuildSubagentName,
    ) -> dict[str, Any]:
        task = task_description.lower()
        ranked_matches = self.rank_existing_subagents(task, subagents)
        if ranked_matches:
            top = ranked_matches[0]
            top_subagent = top["subagent"]
            top_score = int(top["score"])
            second_score = int(ranked_matches[1]["score"]) if len(ranked_matches) > 1 else 0
            margin = top_score - second_score

            if top_score >= self.route_min_score:
                if self.is_confident_ranked_match(ranked_matches):
                    logger.info(
                        "orchestrator route=match strategy=lexical_confident subagent=%s score=%s margin=%s",
                        top_subagent.name,
                        top_score,
                        margin,
                    )
                    return {"branch": "match", "subagent": top_subagent}

                llm_match = await self.llm_validate_ranked_match(
                    task_description,
                    ranked_matches[: self.route_llm_top_k],
                )
                if llm_match:
                    logger.info(
                        "orchestrator route=match strategy=llm_validated subagent=%s top_score=%s margin=%s",
                        llm_match.name,
                        top_score,
                        margin,
                    )
                    return {"branch": "match", "subagent": llm_match}

                logger.info(
                    "orchestrator route=build_new reason=uncertain_match_rejected top_subagent=%s top_score=%s margin=%s",
                    top_subagent.name,
                    top_score,
                    margin,
                )
            else:
                logger.info(
                    "orchestrator route=build_new reason=top_score_below_threshold top_subagent=%s top_score=%s min_score=%s",
                    top_subagent.name,
                    top_score,
                    self.route_min_score,
                )

        new_name = build_new_subagent_name(task_description)
        logger.info("orchestrator route=build_new subagent_name=%s", new_name)
        return {"branch": "build_new", "new_subagent_name": new_name}

    def match_existing_subagent(self, task: str, subagents: list[Subagent]) -> Subagent | None:
        ranked_matches = self.rank_existing_subagents(task, subagents)
        if not ranked_matches:
            return None

        top = ranked_matches[0]
        return top["subagent"] if int(top["score"]) >= self.route_min_score else None

    def rank_existing_subagents(
        self, task: str, subagents: list[Subagent]
    ) -> list[dict[str, Any]]:
        task_tokens = self.tokenize(task)
        if not task_tokens:
            return []

        ranked: list[dict[str, Any]] = []
        for subagent in subagents:
            name_hits = task_tokens.intersection(self.tokenize(subagent.name))
            description_hits = task_tokens.intersection(self.tokenize(subagent.description))
            matched_tokens = name_hits.union(description_hits)
            score = (len(name_hits) * 3) + len(description_hits)
            if score <= 0:
                continue
            ranked.append(
                {
                    "subagent": subagent,
                    "score": score,
                    "name_hits": sorted(name_hits),
                    "description_hits": sorted(description_hits),
                    "matched_token_count": len(matched_tokens),
                }
            )

        ranked.sort(
            key=lambda item: (
                int(item["score"]),
                int(item["matched_token_count"]),
                len(item["name_hits"]),
            ),
            reverse=True,
        )
        return ranked

    def is_confident_ranked_match(self, ranked_matches: list[dict[str, Any]]) -> bool:
        if not ranked_matches:
            return False

        top_score = int(ranked_matches[0]["score"])
        second_score = int(ranked_matches[1]["score"]) if len(ranked_matches) > 1 else 0
        margin = top_score - second_score
        matched_token_count = int(ranked_matches[0]["matched_token_count"])

        return (
            top_score >= self.route_confident_score
            and margin >= self.route_confident_margin
            and matched_token_count >= 2
        )

    async def llm_validate_ranked_match(
        self, task_description: str, ranked_matches: list[dict[str, Any]]
    ) -> Subagent | None:
        if not ranked_matches:
            return None

        if not self.route_llm_enabled:
            logger.info("orchestrator route LLM validation skipped: disabled")
            return None

        if claude_sdk is None:
            logger.info("orchestrator route LLM validation skipped: claude sdk unavailable")
            return None

        candidate_blocks: list[str] = []
        by_name: dict[str, Subagent] = {}
        for idx, item in enumerate(ranked_matches, start=1):
            subagent = item["subagent"]
            by_name[subagent.name.lower()] = subagent
            name_hits = ", ".join(item["name_hits"]) or "(none)"
            description_hits = ", ".join(item["description_hits"]) or "(none)"
            candidate_blocks.append(
                (
                    f"{idx}. name={subagent.name}\n"
                    f"   description={subagent.description}\n"
                    f"   lexical_score={item['score']}\n"
                    f"   name_hits={name_hits}\n"
                    f"   description_hits={description_hits}"
                )
            )

        prompt = (
            "Select an existing subagent ONLY if it clearly fits this task. "
            "If uncertain, choose build_new.\n\n"
            f"TASK:\n{task_description}\n\n"
            "CANDIDATES:\n"
            f"{chr(10).join(candidate_blocks)}\n\n"
            "Return ONLY a JSON object with exactly these keys:\n"
            '{\n'
            '  "decision": "match" or "build_new",\n'
            '  "selected_name": "exact candidate name when decision is match, else empty string",\n'
            '  "confidence": 0.0,\n'
            '  "reason": "short explanation"\n'
            "}\n"
            "Rules:\n"
            "- Do not rely only on lexical overlap.\n"
            "- If capability fit is partial or unclear, choose build_new.\n"
            "- selected_name must exactly match one candidate name when decision=match.\n"
        )

        options = claude_sdk.ClaudeAgentOptions(
            system_prompt=(
                "You are a strict routing validator for subagent selection. "
                "Output strict JSON only."
            ),
            allowed_tools=[],
            max_turns=1,
            permission_mode=self.permission_mode,
        )

        try:
            stream = claude_sdk.query(prompt=prompt, options=options)
            if asyncio.iscoroutine(stream):
                stream = await stream
            response_text = await asyncio.wait_for(
                self.collect_response_text(stream),
                timeout=min(self.timeout_seconds, self.route_llm_timeout_seconds),
            )
        except Exception:
            logger.exception("orchestrator route LLM validation failed")
            return None

        parsed = self.parse_json_object(response_text)
        if not parsed:
            logger.warning("orchestrator route LLM validation returned non-JSON output")
            return None

        decision = str(parsed.get("decision", "")).strip().lower()
        selected_name = str(parsed.get("selected_name", "")).strip()
        confidence = self.normalize_confidence(parsed.get("confidence"))
        reason = self.normalize_text_field(parsed.get("reason"), "", 220)

        if decision != "match":
            logger.info(
                "orchestrator route LLM decision=build_new confidence=%.2f reason=%s",
                confidence,
                self.preview_text(reason),
            )
            return None

        if confidence < self.route_llm_min_confidence:
            logger.info(
                "orchestrator route LLM rejected match due to low confidence=%.2f threshold=%.2f selected=%s reason=%s",
                confidence,
                self.route_llm_min_confidence,
                selected_name,
                self.preview_text(reason),
            )
            return None

        selected = by_name.get(selected_name.lower())
        if selected is None:
            logger.warning(
                "orchestrator route LLM selected unknown subagent=%s; rejecting",
                selected_name,
            )
            return None

        logger.info(
            "orchestrator route LLM accepted subagent=%s confidence=%.2f reason=%s",
            selected.name,
            confidence,
            self.preview_text(reason),
        )
        return selected

    def normalize_confidence(self, value: Any) -> float:
        try:
            number = float(value)
        except Exception:
            return 0.0
        return min(1.0, max(0.0, number))

    def tokenize(self, text: str) -> set[str]:
        raw_tokens = re.findall(r"[a-z0-9]+", text.lower())
        return {token for token in raw_tokens if len(token) > 2 and token not in STOP_WORDS}
