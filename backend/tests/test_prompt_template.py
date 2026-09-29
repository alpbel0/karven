import pytest

from app.prompts.errors import PromptTemplateError
from app.prompts.template import extract_placeholders, render, validate_body


def test_render_substitutes_all_placeholders() -> None:
    body = "Hello ${name}, you are ${role}."
    assert render(body, {"name": "Ada", "role": "admin"}) == "Hello Ada, you are admin."


def test_render_supports_named_syntax() -> None:
    assert render("Hello $name", {"name": "Ada"}) == "Hello Ada"


def test_double_dollar_is_a_literal_dollar() -> None:
    body = "Cost is $$5 for ${item}"
    assert render(body, {"item": "tea"}) == "Cost is $5 for tea"


def test_escaped_dollar_is_not_a_placeholder() -> None:
    assert extract_placeholders("price: $$amount") == set()


def test_missing_variable_raises() -> None:
    with pytest.raises(PromptTemplateError, match="missing"):
        render("Hello ${name} and ${other}", {"name": "Ada"})


def test_extra_variable_raises() -> None:
    with pytest.raises(PromptTemplateError, match="unused"):
        render("Hello ${name}", {"name": "Ada", "other": "x"})


@pytest.mark.parametrize("body", ["$", "${}", "$1abc", "trailing $", "hi ${name"])
def test_invalid_placeholder_syntax_raises(body: str) -> None:
    with pytest.raises(PromptTemplateError):
        extract_placeholders(body)
    with pytest.raises(PromptTemplateError):
        validate_body(body)


def test_extract_placeholders_returns_names() -> None:
    assert extract_placeholders("${a} $b $$c") == {"a", "b"}


def test_validate_body_accepts_valid_template() -> None:
    validate_body("no placeholders here")
    validate_body("only ${one}")
