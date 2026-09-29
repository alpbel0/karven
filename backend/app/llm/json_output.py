"""Strict JSON parsing of model output.

Only trailing chat-template artefacts are stripped. Any other extra content
(prose, code fences, a second object) is a hard error: JSON is never repaired.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.llm.errors import LLMOutputError

logger = logging.getLogger(__name__)

LEAKED_TEMPLATE_TOKENS = (
    "<|im_end|>",
    "<|endoftext|>",
    "<|eot_id|>",
    "<｜end▁of▁sentence｜>",
)


def _strip_template_tokens(text: str) -> str:
    value = text
    while True:
        value = value.strip()
        for token in LEAKED_TEMPLATE_TOKENS:
            if value.endswith(token):
                # Fixed, greppable text: Task 0.5 needs a live occurrence to close.
                logger.warning("llm template token stripped: %s", token)
                value = value[: -len(token)]
                break
        else:
            return value


def parse_json_output(text: str) -> Any:
    """Parse ``text`` as JSON after stripping leaked end-of-turn tokens.

    Raises:
        LLMOutputError: if anything other than known template tokens surrounds
            the JSON document, or if the payload is not valid JSON.
    """
    if text is None:
        raise LLMOutputError("model output was empty")
    cleaned = _strip_template_tokens(text)
    if not cleaned:
        raise LLMOutputError("model output contained no JSON")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"model output is not valid JSON: {exc}") from exc
