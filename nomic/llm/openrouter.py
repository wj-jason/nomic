"""Minimal async OpenRouter client with structured (JSON) output and cost capture."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from nomic.engine.actions import strict_schema

BASE_URL = "https://openrouter.ai/api/v1"
T = TypeVar("T", bound=BaseModel)

# Providers whose models accept explicit cache_control breakpoints via OpenRouter.
# (OpenAI, DeepSeek, Grok, ... cache automatically and need nothing.)
_CACHE_CONTROL_PREFIXES = ("anthropic/", "google/gemini")

# Hidden reasoning counts toward max_tokens, so each effort level gets this much headroom on
# top of the visible-answer cap. Kept tight: OpenRouter reserves max_tokens x price up front.
REASONING_ALLOWANCE = {"none": 0, "minimal": 1024, "low": 2048, "medium": 4096, "high": 8192}


async def _backoff(attempt: int) -> None:  # patched out in tests
    await asyncio.sleep(2**attempt)


class LLMError(RuntimeError):
    def __init__(self, msg: str, records: list[CallRecord] | None = None) -> None:
        super().__init__(msg)
        self.records = records or []  # calls already paid for before the failure


@dataclass
class CallRecord:
    model: str
    purpose: str
    seat: str
    request: list[dict[str, Any]]
    response_text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    latency_ms: int = 0
    error: str | None = None
    generation_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def extract_json(text: str) -> Any:
    """Parse a JSON object out of a model reply, tolerating code fences and chatter."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in reply") from None
        return json.loads(text[start : end + 1])


def _prepare_messages(model: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn our {"cache": True} markers into provider cache breakpoints (or drop them)."""
    use_cc = model.startswith(_CACHE_CONTROL_PREFIXES)
    out = []
    for m in messages:
        m = dict(m)
        cache = m.pop("cache", False)
        if cache and use_cc and isinstance(m.get("content"), str):
            m["content"] = [
                {"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}
            ]
        out.append(m)
    return out


class OpenRouterClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = 180.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 4,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.max_retries = max_retries
        self._no_schema: set[str] = set()  # models that rejected response_format
        self._needs_reasoning: set[str] = set()  # models that refuse effort "none"
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/nomic-arena",
                "X-Title": "Nomic Arena",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        purpose: str,
        seat: str,
        schema: tuple[str, dict[str, Any]] | None = None,
        effort: str = "low",
        max_tokens: int = 800,
    ) -> CallRecord:
        """``max_tokens`` caps the visible answer; reasoning headroom is added per effort."""
        if not self.api_key:
            raise LLMError("OPENROUTER_API_KEY is not set")
        if effort == "none" and model in self._needs_reasoning:
            effort = "minimal"
        body: dict[str, Any] = {
            "model": model,
            "messages": _prepare_messages(model, messages),
            "max_tokens": max_tokens + REASONING_ALLOWANCE.get(effort, 0),
            "reasoning": {"effort": effort, "exclude": True},
            "usage": {"include": True},
        }
        if schema and model not in self._no_schema:
            name, js = schema
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": js},
            }
        rec = CallRecord(model=model, purpose=purpose, seat=seat, request=messages)
        t0 = time.monotonic()
        last_err = ""
        for attempt in range(self.max_retries):
            try:
                resp = await self._http.post("/chat/completions", json=body)
            except httpx.TransportError as e:
                last_err = f"transport error: {e}"
                await _backoff(attempt)
                continue
            if (
                resp.status_code == 400
                and body["reasoning"]["effort"] == "none"
                and "reasoning is mandatory" in resp.text.lower()
            ):
                # Some models can't turn reasoning off; use the smallest effort instead.
                self._needs_reasoning.add(model)
                body["reasoning"] = {"effort": "minimal", "exclude": True}
                body["max_tokens"] = max_tokens + REASONING_ALLOWANCE["minimal"]
                continue
            if resp.status_code == 400 and "response_format" in body:
                # Some providers reject structured outputs; fall back to prompt-only JSON.
                self._no_schema.add(model)
                body.pop("response_format")
                continue
            if resp.status_code in (408, 429) or resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                await _backoff(attempt)
                continue
            if resp.status_code != 200:
                raise LLMError(f"{model}: HTTP {resp.status_code}: {resp.text[:500]}")
            payload = resp.json()
            if "error" in payload:
                last_err = f"provider error: {payload['error']}"
                await _backoff(attempt)
                continue
            self._fill(rec, payload)
            rec.latency_ms = int((time.monotonic() - t0) * 1000)
            return rec
        raise LLMError(f"{model}: gave up after {self.max_retries} attempts ({last_err})")

    @staticmethod
    def _fill(rec: CallRecord, payload: dict[str, Any]) -> None:
        choice = (payload.get("choices") or [{}])[0]
        rec.response_text = (choice.get("message") or {}).get("content") or ""
        rec.generation_id = payload.get("id")
        usage = payload.get("usage") or {}
        rec.prompt_tokens = int(usage.get("prompt_tokens") or 0)
        rec.completion_tokens = int(usage.get("completion_tokens") or 0)
        rec.cached_tokens = int(
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        )
        rec.reasoning_tokens = int(
            (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        )
        rec.cost = float(usage.get("cost") or 0.0)
        rec.extra = {
            "finish_reason": choice.get("finish_reason"),
            "provider": payload.get("provider"),
        }

    async def structured(
        self,
        model: str,
        messages: list[dict[str, Any]],
        schema_model: type[T],
        *,
        purpose: str,
        seat: str,
        effort: str = "low",
        max_tokens: int = 800,
        repairs: int = 2,
    ) -> tuple[T, list[CallRecord]]:
        """Ask for a JSON object matching ``schema_model``; re-prompt with the error on failure.
        Returns the parsed action and every call made (all of them cost money)."""
        schema = (schema_model.__name__, strict_schema(schema_model))
        msgs = list(messages)
        records: list[CallRecord] = []
        for _ in range(repairs + 1):
            try:
                rec = await self.chat(
                    model,
                    msgs,
                    purpose=purpose,
                    seat=seat,
                    schema=schema,
                    effort=effort,
                    max_tokens=max_tokens,
                )
            except LLMError as e:
                raise LLMError(str(e), records) from e
            records.append(rec)
            if rec.extra.get("finish_reason") == "length":
                # Cut off at the cap (typically deliberating inside a field). Don't echo the
                # long reply back; just ask for a shorter one.
                rec.error = "truncated: hit the output token limit"
                msgs = [
                    *msgs,
                    {
                        "role": "user",
                        "content": "Your previous reply was cut off at the length limit before "
                        "the JSON was complete. Reply again, much shorter: keep any reasoning "
                        "to the 3-sentence thinking field and keep rule text under 80 words.",
                    },
                ]
                continue
            try:
                return schema_model.model_validate(extract_json(rec.response_text)), records
            except (ValueError, ValidationError) as e:
                rec.error = f"invalid reply: {e}"
                msgs = [
                    *msgs,
                    {"role": "assistant", "content": rec.response_text or "(empty)"},
                    {
                        "role": "user",
                        "content": f"That reply was not valid ({str(e)[:400]}). Reply again "
                        "with only a JSON object matching the required format.",
                    },
                ]
        raise LLMError(
            f"{model}: no valid {schema_model.__name__} after {repairs + 1} tries", records
        )
