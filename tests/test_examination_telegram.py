# route: (pytest) | file: tests/test_examination_telegram.py
"""Привязка Telegram и обработка сообщений экзамена."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def test_telegram_binding_and_consent_flow() -> None:
    from app.db import SessionLocal
    from skill_assessment.runner import app
    from skill_assessment.services import examination_service as ex
    from skill_assessment.services.telegram_examination import CONSENT_PROMPT, INTRO_PROMPT, handle_telegram_message

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
        assert lines2 and lines2[0] == INTRO_PROMPT
        row = ex.get_or_create_active_examination_session(db, cid, eid)
        from skill_assessment.domain.examination_entities import ExaminationPhase

        assert ExaminationPhase(row.phase).value == "intro"
    finally:
        db.close()
