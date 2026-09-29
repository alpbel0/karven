import pytest

from app.llm.errors import LLMOutputError
from app.llm.json_output import parse_json_output


def test_strips_trailing_template_token() -> None:
    assert parse_json_output('{"a": 1}\n<|im_end|>') == {"a": 1}


def test_strips_repeated_and_mixed_template_tokens() -> None:
    text = '{"a": 1}<|im_end|><|endoftext|> <｜end▁of▁sentence｜>'
    assert parse_json_output(text) == {"a": 1}


def test_strips_surrounding_whitespace() -> None:
    assert parse_json_output('  \n {"a": 1} \n  ') == {"a": 1}


def test_template_token_inside_string_is_preserved() -> None:
    assert parse_json_output('{"a": "<|im_end|>"}') == {"a": "<|im_end|>"}


def test_prose_after_json_raises() -> None:
    with pytest.raises(LLMOutputError):
        parse_json_output('{"a": 1} and here is some prose')


def test_second_object_after_json_raises() -> None:
    with pytest.raises(LLMOutputError):
        parse_json_output('{"a": 1}{"b": 2}')


def test_code_fence_raises() -> None:
    with pytest.raises(LLMOutputError):
        parse_json_output('```json\n{"a": 1}\n```')


def test_empty_output_raises() -> None:
    with pytest.raises(LLMOutputError):
        parse_json_output("")


def test_stripped_template_token_is_logged(caplog) -> None:
    from app.llm.json_output import parse_json_output

    with caplog.at_level("WARNING", logger="app.llm.json_output"):
        assert parse_json_output('{"a": 1}<|im_end|>') == {"a": 1}
    assert "llm template token stripped: <|im_end|>" in caplog.text
