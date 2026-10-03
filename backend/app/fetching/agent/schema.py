"""Final JSON schemas and strict parsers for the data-fetch agent (Task 1.6).

The model answers with one JSON object per call, validated here before any code
acts on it. ``diagnose`` produces a category, a Turkish diagnosis/suggestion and
an optional retry/alternatives proposal; ``ask`` answers a question with a
metadata-only data status. Anything that does not match is an
:class:`~app.llm.errors.LLMOutputError`, which the service records as an agent
error instead of acting on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.llm.errors import LLMOutputError

SYSTEM_PROMPT_KEY = "fetch_agent.system"
ASK_PROMPT_KEY = "fetch_agent.ask"

#: Diagnosis categories (mirrors the ``fetch_failures.category`` CHECK).
DIAGNOSE_CATEGORIES = (
    "no_connector",
    "not_in_source",
    "bad_request",
    "source_error",
    "transient",
    "unknown",
)

#: Categories the code may act on with a retry request.
RETRYABLE_CATEGORIES = ("bad_request", "source_error", "transient")

_RETRY_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "description": (
        "Only when category is bad_request, source_error or transient: the "
        "catalogued institution/dataset and a code per non-time dimension to "
        "fetch again. null when no retry is proposed."
    ),
    "properties": {
        "institution": {"type": ["string", "null"]},
        "dataset": {"type": ["string", "null"]},
        "codes": {
            "type": ["object", "null"],
            "additionalProperties": {"type": "string"},
        },
    },
}

_ALTERNATIVES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": (
        "Catalogued datasets that could satisfy the request instead. Stored for "
        "the admin list only and never fetched automatically."
    ),
    "items": {
        "type": "object",
        "properties": {
            "institution": {"type": "string"},
            "dataset": {"type": "string"},
            "codes": {"type": "object", "additionalProperties": {"type": "string"}},
            "note": {
                "type": "string",
                "description": "Short Turkish note on how it differs (unit, frequency, ...).",
            },
        },
        "required": ["institution", "dataset", "codes", "note"],
    },
}

DIAGNOSE_FINAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": list(DIAGNOSE_CATEGORIES),
            "description": "The single best diagnosis category.",
        },
        "diagnosis": {
            "type": "string",
            "description": "Short Turkish explanation of why the fetch failed.",
        },
        "suggestion": {
            "type": "string",
            "description": "Short Turkish suggestion for the admin.",
        },
        "retry": _RETRY_SCHEMA,
        "alternatives": _ALTERNATIVES_SCHEMA,
    },
    "required": ["category", "diagnosis", "suggestion", "retry", "alternatives"],
}

_DATA_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "available": {
            "type": ["boolean", "null"],
            "description": "Whether the requested data exists; null when unknown.",
        },
        "coverage_start": {"type": ["string", "null"]},
        "coverage_end": {"type": ["string", "null"]},
        "latest_period": {"type": ["string", "null"]},
        "note": {"type": ["string", "null"]},
    },
    "required": ["available", "coverage_start", "coverage_end", "latest_period", "note"],
}

ASK_FINAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Turkish answer to the question."},
        "data_status": _DATA_STATUS_SCHEMA,
        "suggestion": {
            "type": ["string", "null"],
            "description": "Optional short Turkish suggestion.",
        },
    },
    "required": ["answer", "data_status", "suggestion"],
}


@dataclass(frozen=True)
class Diagnosis:
    """A validated diagnosis proposal, ready for code-side validation."""

    category: str
    diagnosis: str
    suggestion: str
    retry: dict[str, Any] | None
    alternatives: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class AskAnswer:
    """A validated answer from :func:`ask_fetch_agent`."""

    answer: str
    data_status: dict[str, Any]
    suggestion: str | None


def _require_object(output: Any, what: str) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise LLMOutputError(f"{what} output is not a JSON object")
    return output


def _require_text(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise LLMOutputError(f"{what} must be a string")
    return value.strip()


def _parse_retry(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LLMOutputError("retry must be an object or null")
    institution = _require_text(raw.get("institution"), "retry.institution")
    dataset = _require_text(raw.get("dataset"), "retry.dataset")
    codes = raw.get("codes")
    if not isinstance(codes, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in codes.items()
    ):
        raise LLMOutputError("retry.codes must be an object of strings")
    if not institution or not dataset or not codes:
        raise LLMOutputError("retry needs institution, dataset and a non-empty codes object")
    return {"institution": institution, "dataset": dataset, "codes": dict(codes)}


def _parse_alternatives(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LLMOutputError("alternatives must be a list")
    alternatives: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise LLMOutputError("each alternative must be an object")
        institution = _require_text(item.get("institution"), "alternative.institution")
        dataset = _require_text(item.get("dataset"), "alternative.dataset")
        codes = item.get("codes")
        if not isinstance(codes, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in codes.items()
        ):
            raise LLMOutputError("alternative.codes must be an object of strings")
        note = item.get("note")
        alternatives.append(
            {
                "institution": institution,
                "dataset": dataset,
                "codes": dict(codes),
                "note": note if isinstance(note, str) else "",
            }
        )
    return alternatives


def parse_diagnosis(output: Any) -> Diagnosis:
    """Validate a diagnose final object, raising :class:`LLMOutputError`."""
    data = _require_object(output, "diagnose")
    category = data.get("category")
    if category not in DIAGNOSE_CATEGORIES:
        raise LLMOutputError(f"unknown diagnosis category {category!r}")
    diagnosis = _require_text(data.get("diagnosis"), "diagnosis")
    suggestion = _require_text(data.get("suggestion"), "suggestion")
    return Diagnosis(
        category=category,
        diagnosis=diagnosis,
        suggestion=suggestion,
        retry=_parse_retry(data.get("retry")),
        alternatives=_parse_alternatives(data.get("alternatives")),
    )


def parse_ask_answer(output: Any) -> AskAnswer:
    """Validate an ask final object, raising :class:`LLMOutputError`."""
    data = _require_object(output, "ask")
    answer = _require_text(data.get("answer"), "answer")
    status = data.get("data_status")
    if not isinstance(status, dict):
        raise LLMOutputError("data_status must be an object")
    available = status.get("available")
    if available is not None and not isinstance(available, bool):
        raise LLMOutputError("data_status.available must be a boolean or null")
    data_status = {
        "available": available,
        "coverage_start": status.get("coverage_start"),
        "coverage_end": status.get("coverage_end"),
        "latest_period": status.get("latest_period"),
        "note": status.get("note"),
    }
    suggestion = data.get("suggestion")
    if suggestion is not None and not isinstance(suggestion, str):
        raise LLMOutputError("suggestion must be a string or null")
    return AskAnswer(answer=answer, data_status=data_status, suggestion=suggestion)


__all__ = [
    "ASK_FINAL_SCHEMA",
    "ASK_PROMPT_KEY",
    "DIAGNOSE_CATEGORIES",
    "DIAGNOSE_FINAL_SCHEMA",
    "RETRYABLE_CATEGORIES",
    "SYSTEM_PROMPT_KEY",
    "AskAnswer",
    "Diagnosis",
    "parse_ask_answer",
    "parse_diagnosis",
]
