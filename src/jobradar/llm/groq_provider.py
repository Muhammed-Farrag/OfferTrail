"""Groq provider (free tier) using strict JSON-schema structured outputs.

Primary model `openai/gpt-oss-120b`, fallback `openai/gpt-oss-20b` on rate limits. Both
support `response_format={"type": "json_schema", "strict": true}` on Groq. Usage is recorded
against a daily token budget so the bot degrades to rule-only analysis instead of failing.
"""

from __future__ import annotations

import json
import sqlite3

from jobradar.llm import budget
from jobradar.llm.base import UNTRUSTED_PREAMBLE, LLMUnavailable

PROMPT_VERSION = "p1"


class GroqProvider:
    def __init__(self, api_key: str, conn: sqlite3.Connection, model: str, fallback: str,
                 daily_tokens: int, timeout_s: float = 30.0) -> None:
        import groq

        self._groq = groq
        self._client = groq.Groq(api_key=api_key, timeout=timeout_s, max_retries=1)
        self._conn = conn
        self.model = model
        self.fallback = fallback
        self.daily_tokens = daily_tokens

    @property
    def model_id(self) -> str:
        return f"groq:{self.model}@{PROMPT_VERSION}"

    def complete_json(self, *, task: str, system: str, data: dict, schema: dict) -> dict:
        if not budget.has_budget(self._conn, self.daily_tokens):
            raise LLMUnavailable("daily LLM token budget reached")
        messages = [
            {"role": "system", "content": f"{system}\n\n{UNTRUSTED_PREAMBLE}"},
            {"role": "user", "content": "<data>\n" + json.dumps(data, ensure_ascii=False)
             + "\n</data>"},
        ]
        last_err: Exception | None = None
        for model in (self.model, self.fallback):
            try:
                resp = self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0,
                    max_completion_tokens=1200,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": task, "schema": schema, "strict": True},
                    },
                )
            except self._groq.RateLimitError as e:
                last_err = e
                budget.record(self._conn, 0, f"rate limited on {model}")
                continue
            except (self._groq.APIConnectionError, self._groq.APIStatusError) as e:
                budget.record(self._conn, 0, f"{type(e).__name__}: {e}"[:300])
                raise LLMUnavailable(str(e)) from e
            tokens = resp.usage.total_tokens if resp.usage else 0
            budget.record(self._conn, tokens)
            try:
                return json.loads(resp.choices[0].message.content or "")
            except json.JSONDecodeError as e:
                raise LLMUnavailable("model returned invalid JSON") from e
        raise LLMUnavailable(f"rate limited on all models: {last_err}")
