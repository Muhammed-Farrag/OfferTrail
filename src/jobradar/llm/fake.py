"""Deterministic offline LLM for tests and the demo.

Each task has a handler that builds its answer *only* from the provided data, so the offline
path exercises the same validation code as a real model. Tests can override handlers to
simulate hostile or broken output.
"""

from __future__ import annotations

from collections.abc import Callable

from jobradar.llm.base import LLMUnavailable


def _draft_email(data: dict) -> dict:
    facts = data.get("approved_facts", [])[:3]
    lines = [f"Dear {data.get('employer') or 'Hiring Team'},", "",
             f"I am applying for the {data.get('job_title')} position."]
    if facts:
        lines += ["", "Relevant experience:"]
        lines += [f"- {f['text']}" for f in facts]
    lines += ["", "My CV is attached. Thank you for your consideration.", "",
              "Best regards,", data.get("candidate_name") or ""]
    return {"body": "\n".join(lines).strip(), "used_fact_ids": [f["id"] for f in facts]}


def _answer(data: dict) -> dict:
    evidence = data.get("evidence", [])
    if not evidence:
        return {"answer": "Unknown — no stored evidence covers this.", "citations": [],
                "unknowns": [data.get("question", "")]}
    top = evidence[:3]
    return {
        "answer": " ".join(f"{e['text']} [{e['id']}]" for e in top),
        "citations": [e["id"] for e in top],
        "unknowns": [],
    }


def _extract_job_facts(data: dict) -> dict:
    return {"sponsorship": "unknown", "sponsorship_quote": "",
            "remote_scope": [], "remote_scope_quote": "",
            "years_min": None, "years_quote": ""}


class FakeLLM:
    def __init__(self, handlers: dict[str, Callable[[dict], dict]] | None = None,
                 unavailable: bool = False) -> None:
        self.handlers: dict[str, Callable[[dict], dict]] = {
            "draft_email": _draft_email,
            "grounded_answer": _answer,
            "extract_job_facts": _extract_job_facts,
        }
        self.handlers.update(handlers or {})
        self.unavailable = unavailable
        self.calls: list[tuple[str, dict]] = []

    @property
    def model_id(self) -> str:
        return "fake@v1"

    def complete_json(self, *, task: str, system: str, data: dict, schema: dict) -> dict:
        self.calls.append((task, data))
        if self.unavailable:
            raise LLMUnavailable("fake LLM configured as unavailable")
        if task not in self.handlers:
            raise LLMUnavailable(f"no fake handler for {task}")
        return self.handlers[task](data)
