"""Tool-calling loop ending in one strict JSON request.

Tool turns send ``tools`` but never ``response_format``. Once the model stops
requesting tools (or ``max_tool_calls`` is reached) exactly one final request is
sent that carries ``response_format`` (a JSON schema) and no ``tools``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.llm.chat import ChatClient, ChatResult
from app.llm.json_output import parse_json_output
from app.prompts.ref import PromptRef

ToolFunction = Callable[..., Any]
ToolSpec = tuple[Mapping[str, Any], ToolFunction]

# The final request must not end with an assistant message: verified live on EVREN
# (2026-09-30) that the model then returns empty content. A closing user
# instruction makes it answer with the JSON. Callers may pass a versioned prompt.
DEFAULT_FINAL_INSTRUCTION = "Now give your final answer as JSON that matches the required schema."


@dataclass
class ToolLoopResult:
    """Result of a completed tool loop."""

    output: Any
    messages: list[dict[str, Any]]
    usage: dict[str, Any]
    final: ChatResult
    tool_calls: int = field(default=0)


def _tool_definitions(tools: Mapping[str, ToolSpec]) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for name, (schema, _function) in tools.items():
        function: dict[str, Any] = {"name": name, "parameters": dict(schema)}
        description = schema.get("description") if isinstance(schema, Mapping) else None
        if description:
            function["description"] = description
        definitions.append({"type": "function", "function": function})
    return definitions


def _execute_tool(call: Mapping[str, Any], tools: Mapping[str, ToolSpec]) -> dict[str, Any]:
    function = call.get("function") or {}
    name = function.get("name")
    call_id = call.get("id")
    entry = tools.get(name)
    if entry is None:
        content = json.dumps({"error": f"unknown tool: {name}"})
    else:
        try:
            raw_arguments = function.get("arguments") or "{}"
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            output = entry[1](**arguments)
            content = output if isinstance(output, str) else json.dumps(output, default=str)
        except Exception as exc:  # noqa: BLE001 - report tool errors to the model
            content = json.dumps({"error": str(exc)})
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _accumulate_usage(total: dict[str, Any], usage: dict[str, Any] | None) -> None:
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, bool):
            total[key] = value
        elif isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value
        elif key not in total:
            total[key] = value


def run_tool_loop(
    client: ChatClient,
    messages: Sequence[dict[str, Any]],
    tools: Mapping[str, ToolSpec],
    final_schema: Mapping[str, Any],
    *,
    max_tool_calls: int,
    final_instruction: str = DEFAULT_FINAL_INSTRUCTION,
    prompt_ref: PromptRef | None = None,
) -> ToolLoopResult:
    """Run the tool loop and return the parsed final JSON object."""
    transcript: list[dict[str, Any]] = [dict(message) for message in messages]
    tool_definitions = _tool_definitions(tools)
    total_usage: dict[str, Any] = {}
    executed_calls = 0

    while executed_calls < max_tool_calls:
        result = client.complete(transcript, tools=tool_definitions, prompt_ref=prompt_ref)
        _accumulate_usage(total_usage, result.usage)

        if not result.tool_calls:
            transcript.append({"role": "assistant", "content": result.content or ""})
            break

        remaining = max_tool_calls - executed_calls
        selected_calls = result.tool_calls[:remaining]
        transcript.append(
            {
                "role": "assistant",
                "content": result.content or "",
                "tool_calls": selected_calls,
            }
        )
        for call in selected_calls:
            transcript.append(_execute_tool(call, tools))
            executed_calls += 1

    transcript.append({"role": "user", "content": final_instruction})
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "final_output", "schema": dict(final_schema)},
    }
    final = client.complete(transcript, response_format=response_format, prompt_ref=prompt_ref)
    _accumulate_usage(total_usage, final.usage)
    output = parse_json_output(final.content or "")

    return ToolLoopResult(
        output=output,
        messages=transcript,
        usage=total_usage,
        final=final,
        tool_calls=executed_calls,
    )
