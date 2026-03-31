from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


class _FakeDb:
    def __init__(self) -> None:
        self.commits = 0
        self.refreshes = 0

    def commit(self) -> None:
        self.commits += 1

    def refresh(self, _row) -> None:
        self.refreshes += 1

    def close(self) -> None:
        return None


def test_declined_pd_consent_cancels_assessment_session(monkeypatch) -> None:
    from skill_assessment.services import telegram_docs_survey_consent as mod

    now_before = datetime.now(timezone.utc)
    row = SimpleNamespace(
        id="s1",
        status="in_progress",
        phase="part1",
        docs_survey_pd_consent_status="awaiting_first",
        docs_survey_exam_gate_awaiting=True,
        docs_survey_hr_notified_no_consent_at=None,
    )
    db = _FakeDb()
    monkeypatch.setattr(mod, "_examination_blocks_pd_consent", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(mod, "_find_awaiting_first_consent_session", lambda *_args, **_kwargs: row)
    monkeypatch.setattr(mod, "notify_hr_docs_survey_consent_issue", lambda *_args, **_kwargs: False)

    out = mod.handle_docs_survey_pd_consent_message(db, "300398364", "нет", False)

    assert out and "Отказ от согласия зафиксирован" in out[0][0]
    assert row.docs_survey_pd_consent_status == "declined"
    assert row.status == "cancelled"
    assert row.phase == "part1"
    assert row.docs_survey_exam_gate_awaiting is False
    assert row.completed_at >= now_before


def test_consent_timeout_cancels_assessment_session(monkeypatch) -> None:
    from skill_assessment.services import docs_survey_consent_timeout as mod

    row = SimpleNamespace(
        id="sess-timeout",
        docs_survey_pd_consent_status="awaiting_first",
        docs_survey_hr_notified_no_consent_at=None,
        docs_survey_exam_gate_awaiting=True,
    )
    db = _FakeDb()

    class _Rows:
        def all(self):
            return [row]

    db.scalars = lambda _q: _Rows()  # type: ignore[attr-defined]
    monkeypatch.setattr(mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(mod, "notify_hr_docs_survey_consent_issue", lambda *_args, **_kwargs: False)

    processed = mod.process_consent_timeouts_once()

    assert processed == 1
    assert row.docs_survey_pd_consent_status == "timed_out"
    assert row.status == "cancelled"
    assert row.phase == "part1"
    assert row.docs_survey_exam_gate_awaiting is False
    assert row.completed_at is not None


def test_exam_answer_timeout_cancels_latest_part1_session(monkeypatch) -> None:
    from skill_assessment.services import examination_service as ex

    db = _FakeDb()
    row = SimpleNamespace(
        id="exam1",
        phase="questions",
        status="in_progress",
        client_id="c1",
        employee_id="e1",
        completed_at=None,
    )
    called: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ex,
        "_cancel_latest_part1_assessment_session_for_pair",
        lambda _db, client_id, employee_id: called.append((client_id, employee_id)) or True,
    )

    ok = ex._interrupt_for_answer_timeout(
        db,
        row,
        last_answer_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        notify_hr=False,
    )

    assert ok is True
    assert row.phase == "interrupted_timeout"
    assert row.status == "cancelled"
    assert called == [("c1", "e1")]
