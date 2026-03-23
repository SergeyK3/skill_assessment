# route: (tests) | file: tests/test_docs_survey_consent.py
"""Согласие ПДн для Part1 (парсер ответов)."""

from skill_assessment.services.telegram_docs_survey_consent import _parse_pd_consent_yes_no


def test_parse_pd_consent_yes_no() -> None:
    assert _parse_pd_consent_yes_no("да") is True
    assert _parse_pd_consent_yes_no("Согласен") is True
    assert _parse_pd_consent_yes_no("нет") is False
    assert _parse_pd_consent_yes_no("отказ") is False
    assert _parse_pd_consent_yes_no("готов") is None
    assert _parse_pd_consent_yes_no("") is None
    assert _parse_pd_consent_yes_no("не знаю") is None
