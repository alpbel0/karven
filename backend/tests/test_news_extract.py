"""Unit tests for full article text extraction (Task 1.7).

Each real article fixture (the 2nd feed item's page, saved 2026-10-03) must
extract to at least the minimum text length, and Sabah's header lines must be
gone. No network is touched.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.news import extract as extract_module
from app.news.extract import extract_text, extract_with_error

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "news"
SOURCES = ("sabah", "haberturk", "sozcu", "bloomberght", "cnnturk")


@pytest.mark.parametrize("source", SOURCES)
def test_real_article_extracts_full_text(source: str) -> None:
    html = (FIXTURES / f"{source}.article.raw.html").read_bytes()
    text = extract_text(html)
    assert text is not None
    assert len(text) >= 200


def test_sabah_header_lines_are_stripped() -> None:
    html = (FIXTURES / "sabah.article.raw.html").read_bytes()
    text = extract_text(html)
    assert text is not None
    assert not text.startswith("Giriş Tarihi:")
    assert not text.startswith("Son Güncelleme:")
    assert "Giriş Tarihi:" not in text.splitlines()[0]


def test_tiny_html_yields_none_or_short() -> None:
    text = extract_text("<html><body><p>Merhaba</p></body></html>")
    assert text is None or len(text) < 200


def test_empty_input_yields_none() -> None:
    assert extract_text(b"") is None
    assert extract_text("") is None


def test_header_only_html_yields_none() -> None:
    html = (
        "<html><body><article><p>Giriş Tarihi: 3.10.2026</p>"
        "<p>Son Güncelleme: 3.10.2026</p></article></body></html>"
    )
    assert extract_text(html) is None


def test_extract_with_error_is_clean_on_success() -> None:
    html = (FIXTURES / "sabah.article.raw.html").read_bytes()
    result = extract_with_error(html)
    assert result.text is not None
    assert result.error is None


def test_trafilatura_exception_is_logged_and_reported(monkeypatch, caplog) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("trafilatura exploded")

    monkeypatch.setattr(extract_module.trafilatura, "extract", boom)

    with caplog.at_level(logging.WARNING, logger="app.news.extract"):
        result = extract_with_error("<html><body><p>x</p></body></html>")

    assert result.text is None
    assert result.error == "RuntimeError: trafilatura exploded"
    assert any("news text extraction failed" in record.message for record in caplog.records)
