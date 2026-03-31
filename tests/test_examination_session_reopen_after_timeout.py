from __future__ import annotations

from types import SimpleNamespace


def test_get_or_create_creates_new_session_when_existing_turns_interrupted_timeout(monkeypatch) -> None:
    from skill_assessment.services import examination_service as ex

    existing = SimpleNamespace(
        id="old-session",
        phase="questions",
        status="in_progress",
    )
    created = SimpleNamespace(
        id="new-session",
        phase="consent",
        status="scheduled",
    )

    class _FakeScalar:
        def first(self):
            return existing

    class _FakeDb:
        def scalars(self, _query):
            return _FakeScalar()

    monkeypatch.setattr(ex, "_question_count", lambda *_a, **_k: 1)

    def _fake_ensure(_db, row):
        row.phase = "interrupted_timeout"
        row.status = "cancelled"
        return row

    monkeypatch.setattr(ex, "ensure_not_answer_timed_out", _fake_ensure)
    monkeypatch.setattr(ex, "_insert_examination_session_row", lambda *_a, **_k: created)
    monkeypatch.setattr(ex, "seed_session_questions_from_hr", lambda *_a, **_k: None)

    got = ex.get_or_create_active_examination_session(_FakeDb(), "client1", "employee1")
    assert got.id == "new-session"
