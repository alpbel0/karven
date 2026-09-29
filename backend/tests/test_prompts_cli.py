import pytest

from app.prompts.cli import build_parser


def test_add_parses_key_file_and_note(tmp_path) -> None:
    args = build_parser().parse_args(
        ["add", "--key", "first_agent.system", "--file", str(tmp_path / "p.txt"), "--note", "n"]
    )
    assert args.command == "add"
    assert args.key == "first_agent.system"
    assert args.note == "n"
    assert str(args.file).endswith("p.txt")


def test_add_note_is_optional(tmp_path) -> None:
    args = build_parser().parse_args(["add", "--key", "a.b", "--file", str(tmp_path / "p.txt")])
    assert args.note is None


def test_add_requires_file(tmp_path) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["add", "--key", "a.b"])


def test_activate_parses_int_version() -> None:
    args = build_parser().parse_args(["activate", "--key", "a.b", "--version", "3"])
    assert args.command == "activate"
    assert args.version == 3


def test_activate_requires_version() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["activate", "--key", "a.b"])


def test_list_key_is_optional() -> None:
    args = build_parser().parse_args(["list"])
    assert args.command == "list"
    assert args.key is None


def test_show_defaults_to_active() -> None:
    args = build_parser().parse_args(["show", "--key", "a.b"])
    assert args.command == "show"
    assert args.version is None


def test_show_accepts_version() -> None:
    args = build_parser().parse_args(["show", "--key", "a.b", "--version", "2"])
    assert args.version == 2


def test_unknown_command_exits() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["frobnicate"])
