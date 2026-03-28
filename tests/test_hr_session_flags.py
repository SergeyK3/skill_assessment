# route: (pytest) | file: tests/test_hr_session_flags.py
"""Метка «неявка» в списке HR: :func:`skill_assessment.services.hr_session_flags.is_hr_no_show`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


def _naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


@pytest.fixture()
def past_slot() -> datetime:
    return _naive_utc_now() - timedelta(days=1)


@pytest.fixture()
def future_slot() -> datetime:
    return _naive_utc_now() + timedelta(days=1)


def test_no_show_completed_false() -> None:
    from skill_assessment.services.hr_session_flags import is_hr_no_show

    assert is_hr_no_show(
        _row(
            status="completed",
            phase="completed",
            docs_survey_pd_consent_status="timed_out",
            docs_survey_scheduled_at=datetime(2000, 1, 1),
        )
    ) is False


def test_no_show_cancelled_past_slot_part_phase_true(past_slot: datetime) -> None:
    from skill_assessment.services.hr_session_flags import is_hr_no_show

    assert is_hr_no_show(
        _row(
            status="cancelled",
            phase="part1",
            docs_survey_pd_consent_status="accepted",
            docs_survey_scheduled_at=past_slot,
        )
    ) is True


def test_no_show_cancelled_timed_out_true() -> None:
    from skill_assessment.services.hr_session_flags import is_hr_no_show

    assert is_hr_no_show(
        _row(
            status="cancelled",
            phase="part1",
            docs_survey_pd_consent_status="timed_out",
            docs_survey_scheduled_at=None,
        )
    ) is True


def test_no_show_cancelled_report_past_slot_false(past_slot: datetime) -> None:
    from skill_assessment.services.hr_session_flags import is_hr_no_show

    assert is_hr_no_show(
        _row(
            status="cancelled",
            phase="report",
            docs_survey_pd_consent_status="accepted",
            docs_survey_scheduled_at=past_slot,
        )
    ) is False


def test_no_show_timed_out_consent_true() -> None:
    from skill_assessment.services.hr_session_flags import is_hr_no_show

    assert is_hr_no_show(
        _row(
            status="in_progress",
            phase="part1",
            docs_survey_pd_consent_status="timed_out",
            docs_survey_scheduled_at=None,
        )
    ) is True


def test_no_show_past_slot_in_progress_part_phases_true(past_slot: datetime) -> None:
    from skill_assessment.services.hr_session_flags import is_hr_no_show

    assert is_hr_no_show(
        _row(
            status="in_progress",
            phase="part1",
            docs_survey_pd_consent_status="accepted",
            docs_survey_scheduled_at=past_slot,
            docs_survey_readiness_answer=None,
            part1_docs_checklist_json=None,
        )
    ) is True


def test_no_show_past_slot_ready_without_exam_progress_true(past_slot: datetime) -> None:
    from skill_assessment.services.hr_session_flags import is_hr_no_show

    assert is_hr_no_show(
        _row(
            status="in_progress",
            phase="part1",
            docs_survey_pd_consent_status="accepted",
            docs_survey_scheduled_at=past_slot,
            docs_survey_readiness_answer="ready",
            part1_docs_checklist_json=None,
        )
    ) is True


def test_no_show_past_slot_with_exam_progress_false(monkeypatch: pytest.MonkeyPatch, past_slot: datetime) -> None:
    import skill_assessment.services.hr_session_flags as mod

    monkeypatch.setattr(mod, "_has_exam_progress", lambda db, row: True)

    assert mod.is_hr_no_show(
        _row(
            client_id="c1",
            employee_id="e1",
            created_at=_naive_utc_now(),
            status="in_progress",
            phase="part1",
            docs_survey_pd_consent_status="accepted",
            docs_survey_scheduled_at=past_slot,
            docs_survey_readiness_answer="ready",
            part1_docs_checklist_json=None,
        ),
        db=None,
    ) is False


def test_no_show_past_slot_part2_part3_false(past_slot: datetime) -> None:
    from skill_assessment.services.hr_session_flags import is_hr_no_show

    for ph in ("part2", "part3"):
        assert is_hr_no_show(
            _row(
                status="in_progress",
                phase=ph,
                docs_survey_pd_consent_status="accepted",
                docs_survey_scheduled_at=past_slot,
                docs_survey_readiness_answer=None,
                part1_docs_checklist_json=None,
            )
        ) is False, ph


def test_no_show_future_slot_false(future_slot: datetime) -> None:
    from skill_assessment.services.hr_session_flags import is_hr_no_show

    assert is_hr_no_show(
        _row(
            status="in_progress",
            phase="part1",
            docs_survey_pd_consent_status="accepted",
            docs_survey_scheduled_at=future_slot,
        )
    ) is False


def test_no_show_report_phase_false_even_if_past_slot(past_slot: datetime) -> None:
    from skill_assessment.services.hr_session_flags import is_hr_no_show

    assert is_hr_no_show(
        _row(
            status="in_progress",
            phase="report",
            docs_survey_pd_consent_status="accepted",
            docs_survey_scheduled_at=past_slot,
        )
    ) is False


def test_no_show_draft_past_slot_false(past_slot: datetime) -> None:
    from skill_assessment.services.hr_session_flags import is_hr_no_show

    assert is_hr_no_show(
        _row(
            status="draft",
            phase="draft",
            docs_survey_pd_consent_status=None,
            docs_survey_scheduled_at=past_slot,
        )
    ) is False
