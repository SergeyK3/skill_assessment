# route: (pytest) | file: tests/test_examination_telegram.py
"""Привязка Telegram и обработка сообщений экзамена."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def test_telegram_binding_and_consent_flow() -> None:
    from app.db import SessionLocal
    from skill_assessment.runner import app
    from skill_assessment.services import examination_service as ex
    from skill_assessment.services.telegram_examination import CONSENT_PROMPT, handle_telegram_message

    u = uuid.uuid4().hex[:12]
    cid, eid, chat = f"tg_c_{u}", f"tg_e_{u}", f"9{u}"[:12]

    with TestClient(app) as client:
        r = client.post(
            "/api/skill-assessment/examination/telegram/bindings",
            json={
                "client_id": cid,
                "employee_id": eid,
                "telegram_chat_id": chat,
            },
        )
        assert r.status_code == 200
        assert r.json()["telegram_chat_id"] == chat

    db = SessionLocal()
    try:
        lines = handle_telegram_message(db, chat, "/start", True)
        assert lines == [CONSENT_PROMPT]
        lines2 = handle_telegram_message(db, chat, "да", False)
        assert lines2 and "Вопрос 1:" in lines2[0]
        row = ex.get_or_create_active_examination_session(db, cid, eid)
        from skill_assessment.domain.examination_entities import ExaminationPhase

        assert ExaminationPhase(row.phase).value == "questions"
    finally:
        db.close()


def test_examination_resolves_employee_via_docs_survey_chat_without_binding_post() -> None:
    """Тот же чат, что в Part1 (docs_survey_notify_chat_id) — без отдельной POST /telegram/bindings."""
    import uuid

    from app.db import SessionLocal

    from skill_assessment.infrastructure.db_models import AssessmentSessionRow
    from skill_assessment.services.telegram_examination import CONSENT_PROMPT, handle_telegram_message

    u = uuid.uuid4().hex[:12]
    cid, eid, chat = f"ds_c_{u}", f"ds_e_{u}", f"7{u}"[:12]
    sid = str(uuid.uuid4())

    db = SessionLocal()
    try:
        row = AssessmentSessionRow(
            id=sid,
            client_id=cid,
            employee_id=eid,
            status="in_progress",
            phase="part1",
            docs_survey_notify_chat_id=chat,
        )
        db.add(row)
        db.commit()

        lines = handle_telegram_message(db, chat, "/start", True)
        assert lines == [CONSENT_PROMPT]
    finally:
        db.close()


def test_examination_interrupts_when_pause_between_answers_exceeds_five_minutes(monkeypatch) -> None:
    from fastapi import HTTPException

    from skill_assessment.bootstrap import ensure_typical_infrastructure_on_path

    ensure_typical_infrastructure_on_path()
    from app.db import SessionLocal

    from skill_assessment.schemas.examination_api import (
        ExaminationAnswerBody,
        ExaminationConsentBody,
        ExaminationIntroDoneBody,
        ExaminationSessionCreate,
    )
    from skill_assessment.services import examination_service as ex
    from skill_assessment.services.examination_seed import ensure_examination_questions
    import skill_assessment.services.examination_regulation_notify as notify_mod

    notified: list[str] = []

    def _fake_notify(db, row, *, last_answer_at, timeout_minutes):
        notified.append(f"{row.id}:{timeout_minutes}")

    monkeypatch.setattr(notify_mod, "notify_hr_examination_timeout", _fake_notify)

    db = SessionLocal()
    try:
        ensure_examination_questions(db)
        out = ex.create_examination_session(
            db,
            ExaminationSessionCreate(client_id="timeout_client", employee_id="timeout_employee", scenario_id="regulation_v1"),
        )
        sid = out.id
        ex.post_consent(db, sid, ExaminationConsentBody(accepted=True))
        ex.post_intro_done(db, sid, ExaminationIntroDoneBody())
        ex.post_answer(db, sid, ExaminationAnswerBody(transcript_text="Первый ответ"))

        row = db.get(ex.ExaminationSessionRow, sid)
        assert row is not None
        first_answer = row.answers[0]
        first_answer.created_at = datetime.now(timezone.utc) - timedelta(minutes=6)
        db.commit()

        try:
            ex.post_answer(db, sid, ExaminationAnswerBody(transcript_text="Второй ответ"))
            assert False, "expected timeout interruption"
        except HTTPException as err:
            assert err.status_code == 403
            assert err.detail == "examination_interrupted_timeout"

        row = db.get(ex.ExaminationSessionRow, sid)
        assert row is not None
        assert row.phase == "interrupted_timeout"
        assert row.status == "cancelled"
        assert row.completed_at is not None
        assert notified == [f"{sid}:5"]
    finally:
        db.close()


def test_telegram_autocompletes_and_hides_detailed_protocol_after_last_answer() -> None:
    from skill_assessment.bootstrap import ensure_typical_infrastructure_on_path

    ensure_typical_infrastructure_on_path()
    from app.db import SessionLocal
    from skill_assessment.runner import app
    from skill_assessment.services.telegram_examination import handle_telegram_message

    u = uuid.uuid4().hex[:12]
    cid, eid, chat = f"tgp_c_{u}", f"tgp_e_{u}", f"8{u}"[:12]

    with TestClient(app) as client:
        r = client.post(
            "/api/skill-assessment/examination/telegram/bindings",
            json={
                "client_id": cid,
                "employee_id": eid,
                "telegram_chat_id": chat,
            },
        )
        assert r.status_code == 200

    db = SessionLocal()
    try:
        handle_telegram_message(db, chat, "/start", True)
        lines = handle_telegram_message(db, chat, "да", False)
        assert lines and "Вопрос 1:" in lines[0]
        last = []
        for i in range(5):
            last = handle_telegram_message(db, chat, f"Ответ {i + 1}", False)
        assert last
        assert any("Опрос по регламентам завершён" in x for x in last)
        assert any("кейс" in x.lower() for x in last)
        assert not any("Оценка по ответу:" in x for x in last)
        assert not any("Вопрос 1" in x for x in last)
    finally:
        db.close()


def test_get_or_create_active_examination_session_filters_only_active_statuses(monkeypatch) -> None:
    from skill_assessment.services import examination_service as ex

    captured = {"sql": ""}

    class _FakeScalarResult:
        def first(self):
            return object()

    class _FakeDb:
        def scalars(self, query):
            captured["sql"] = str(query.compile(compile_kwargs={"literal_binds": True}))
            return _FakeScalarResult()

    monkeypatch.setattr(ex, "_question_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(ex, "ensure_not_answer_timed_out", lambda db, row: row)

    _ = ex.get_or_create_active_examination_session(_FakeDb(), "client_x", "employee_y")

    sql = captured["sql"]
    assert " IN " in sql
    assert "'scheduled'" in sql
    assert "'in_progress'" in sql
    assert "'cancelled'" not in sql


def test_completed_examination_protocol_html_includes_related_part2_fragment(monkeypatch) -> None:
    from skill_assessment.runner import app

    monkeypatch.setenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND", "mock")
    monkeypatch.setenv("SKILL_ASSESSMENT_PUBLIC_BASE_URL", "https://example.test")

    u = uuid.uuid4().hex[:8]
    cid, eid = f"proto_c_{u}", f"proto_e_{u}"

    with TestClient(app) as client:
        r = client.post("/api/skill-assessment/sessions", json={"client_id": cid, "employee_id": eid})
        assert r.status_code == 200
        sa_session_id = r.json()["id"]

        r = client.post(f"/api/skill-assessment/sessions/{sa_session_id}/start")
        assert r.status_code == 200

        r = client.post(
            "/api/skill-assessment/examination/sessions",
            json={"client_id": cid, "employee_id": eid, "scenario_id": "regulation_v1"},
        )
        assert r.status_code == 200
        exam_session_id = r.json()["id"]

        r = client.post(
            f"/api/skill-assessment/examination/sessions/{exam_session_id}/consent",
            json={"accepted": True},
        )
        assert r.status_code == 200

        r = client.post(f"/api/skill-assessment/examination/sessions/{exam_session_id}/intro/done")
        assert r.status_code == 200

        for i in range(5):
            r = client.post(
                f"/api/skill-assessment/examination/sessions/{exam_session_id}/answer",
                json={"transcript_text": f"Ответ сотрудника {i + 1} по регламенту"},
            )
            assert r.status_code == 200

        r = client.post(f"/api/skill-assessment/examination/sessions/{exam_session_id}/complete")
        assert r.status_code == 200

        r = client.get(f"/api/skill-assessment/examination/sessions/{exam_session_id}/protocol/html")
        assert r.status_code == 200
        assert "Связанный общий протокол оценки" in r.text
        assert "Этап 2 (кейсы):" in r.text
        assert "кейсы назначены" in r.text
        assert "https://example.test/api/skill-assessment/public/report/html?token=" in r.text
