"""Strict ``string.Template`` rendering for prompt bodies.

Placeholders use ``${name}`` syntax with ``$$`` escaping a literal ``$``. Every
placeholder must be provided and every provided variable must appear in the
body; any mismatch raises :class:`PromptTemplateError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from string import Template

from app.prompts.errors import PromptTemplateError


def extract_placeholders(body: str) -> set[str]:
    """Return every placeholder name in ``body``, raising on invalid syntax."""
    names: set[str] = set()
    for match in Template.pattern.finditer(body):
        if match.group("invalid") is not None:
            raise PromptTemplateError(
                "invalid placeholder syntax (expected ${name}; use $$ for a literal '$')"
            )
        name = match.group("named") or match.group("braced")
        if name is not None:
            names.add(name)
    return names


def validate_body(body: str) -> None:
    """Raise :class:`PromptTemplateError` if the body has invalid placeholder syntax."""
    extract_placeholders(body)


def render(body: str, variables: Mapping[str, object]) -> str:
    """Substitute ``variables`` into ``body``, strictly matching placeholders."""
    placeholders = extract_placeholders(body)
    provided = set(variables)
    missing = sorted(placeholders - provided)
    if missing:
        raise PromptTemplateError(f"missing template variables: {', '.join(missing)}")
    extra = sorted(provided - placeholders)
    if extra:
        raise PromptTemplateError(f"unused template variables: {', '.join(extra)}")
    try:
        return Template(body).substitute(dict(variables))
    except (KeyError, ValueError) as exc:
        raise PromptTemplateError(f"template rendering failed: {exc}") from exc
