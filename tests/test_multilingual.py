"""Tests for multilingual helpers."""

from app.multilingual import (
    language_name,
    normalize_language_code,
    parse_target_languages,
)


def test_normalize_language_code_accepts_expected_values():
    assert normalize_language_code("RU") == "ru"
    assert normalize_language_code("en_US") == "en-us"
    assert normalize_language_code("pt-br") == "pt-br"


def test_normalize_language_code_rejects_invalid_values():
    assert normalize_language_code("") == ""
    assert normalize_language_code("russian") == ""
    assert normalize_language_code("e") == ""


def test_parse_target_languages_uses_defaults_and_unique_values():
    assert parse_target_languages(None) == ["ru", "en"]
    assert parse_target_languages("ru,en,ru") == ["ru", "en"]
    assert parse_target_languages("EN, pt-BR") == ["en", "pt-br"]


def test_language_name_returns_known_and_fallback():
    assert language_name("ru") == "Russian"
    assert language_name("en") == "English"
    assert language_name("fr") == "fr"
