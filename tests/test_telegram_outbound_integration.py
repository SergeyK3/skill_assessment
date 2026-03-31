# route: (pytest) | file: tests/test_telegram_outbound_integration.py
"""Интеграция: старт сессии вызывает исходящее «сообщение» в заглушку Telegram (без сети)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


def test_start_session_records_fake_telegram_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
    from skill_assessment.runner import app

    monkeypatch.setenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND", "mock")
    outbound = get_telegram_outbound()
    assert outbound.__class__.__name__ == "FakeTelegramOutbound"
    outbound.clear()
    client_id = "c_tg_" + uuid.uuid4().hex[:8]
    employee_id = "e_tg_" + uuid.uuid4().hex[:8]

    with TestClient(app) as client:
        r = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": employee_id})
        assert r.status_code == 200
        session_id = r.json()["id"]
        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200
        body = r.json()
        tg = body.get("docs_survey_telegram", {})
        assert tg.get("sent") is True
        assert tg.get("used_fallback_chat") is True
        assert len(outbound.messages) >= 1
        assert "опрос по служебным документам" in outbound.messages[-1]["text"]


def test_docs_survey_disallow_fallback_skips_send(monkeypatch: pytest.MonkeyPatch) -> None:
    from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
    from skill_assessment.runner import app

    monkeypatch.setenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND", "mock")
    monkeypatch.setenv("TELEGRAM_DOCS_SURVEY_DISALLOW_FALLBACK", "1")
    outbound = get_telegram_outbound()
    outbound.clear()
    client_id = "c_tg_nofb_" + uuid.uuid4().hex[:8]
    employee_id = "e_tg_nofb_" + uuid.uuid4().hex[:8]

    with TestClient(app) as client:
        r = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": employee_id})
        session_id = r.json()["id"]
        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200
        tg = r.json().get("docs_survey_telegram", {})
        assert tg.get("sent") is False
        assert tg.get("skipped_reason") == "no_employee_telegram_disallow_fallback"
        assert len(outbound.messages) == 0


def test_resend_docs_survey_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
    from skill_assessment.runner import app

    monkeypatch.setenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND", "mock")
    outbound = get_telegram_outbound()
    outbound.clear()
    client_id = "c_tg_rs_" + uuid.uuid4().hex[:8]
    employee_id = "e_tg_rs_" + uuid.uuid4().hex[:8]

    with TestClient(app) as client:
        r = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": employee_id})
        session_id = r.json()["id"]
        client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        n1 = len(outbound.messages)
        r2 = client.post(f"/api/skill-assessment/sessions/{session_id}/resend-docs-survey-telegram")
        assert r2.status_code == 200
        body = r2.json()
        assert body.get("sent") is True
        assert len(outbound.messages) == n1 + 1
        assert "опрос по служебным документам" in outbound.messages[-1]["text"]


def test_complete_part1_sends_part2_case_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
    from skill_assessment.runner import app

    monkeypatch.setenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND", "mock")
    monkeypatch.setenv("SKILL_ASSESSMENT_PUBLIC_BASE_URL", "https://example.test")
    outbound = get_telegram_outbound()
    assert outbound.__class__.__name__ == "FakeTelegramOutbound"
    outbound.clear()
    client_id = "c_tg_case_" + uuid.uuid4().hex[:8]
    employee_id = "e_tg_case_" + uuid.uuid4().hex[:8]

    with TestClient(app) as client:
        r = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": employee_id})
        assert r.status_code == 200
        session_id = r.json()["id"]

        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist")
        assert r.status_code == 200
        answers = {q["id"]: "partial" for q in r.json()["questions"]}

        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist",
            json={"answers": answers, "complete": True},
        )
        assert r.status_code == 200
        assert len(outbound.messages) >= 2
        combined = "\n".join(m["text"] for m in outbound.messages)
        cl = combined.lower()
        assert ("этап 2" in cl) or ("часть 2" in cl)
        assert "кейс 1 из" in cl
        assert "навыки в фокусе" not in cl
        assert "заголовок:" not in cl
        assert "основной навык" not in cl
        assert "связанные навыки" not in cl
        assert "part2-case?token=" not in cl


def test_complete_part2_sends_manager_assessment_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
    from skill_assessment.integration.hr_core import EmployeeSnapshot
    from skill_assessment.runner import app
    from skill_assessment.services import manager_assessment as manager_assessment_svc

    monkeypatch.setenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND", "mock")
    monkeypatch.setenv("TELEGRAM_EXAM_MANAGER_CHAT_ID", "mgr_chat_001")
    monkeypatch.setenv("SKILL_ASSESSMENT_PUBLIC_BASE_URL", "https://example.test")

    def _fake_get_employee(db, client_id, employee_id):
        return EmployeeSnapshot(
            id=str(employee_id or "emp"),
            client_id=str(client_id),
            display_name="Иванов Иван Иванович",
            position_label="менеджер по продажам",
            position_code="SALES_MANAGER",
            department_code="SALES",
        )

    monkeypatch.setattr(manager_assessment_svc, "get_employee", _fake_get_employee)
    monkeypatch.setattr(
        manager_assessment_svc,
        "get_examination_kpi_labels",
        lambda db, client_id, employee_id: [
            "Точность бухгалтерского учета",
            "Своевременное закрытие периода",
            "Снижение налоговых рисков",
        ],
    )

    outbound = get_telegram_outbound()
    assert outbound.__class__.__name__ == "FakeTelegramOutbound"
    outbound.clear()
    client_id = "c_tg_mgr_" + uuid.uuid4().hex[:8]
    employee_id = "e_tg_mgr_" + uuid.uuid4().hex[:8]

    with TestClient(app) as client:
        r = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": employee_id})
        assert r.status_code == 200
        session_id = r.json()["id"]

        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200
        token = r.json().get("part1_docs_checklist_token")
        assert token and len(token) > 16

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist")
        assert r.status_code == 200
        answers = {q["id"]: "partial" for q in r.json()["questions"]}
        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist",
            json={"answers": answers, "complete": True},
        )
        assert r.status_code == 200

        r = client.get("/api/skill-assessment/public/part2-cases?token=" + token)
        assert r.status_code == 200
        payload = r.json()
        submit_answers = {
            "answers": [
                {"case_id": item["case_id"], "answer": "Развернутый ответ по кейсу с KPI и регламентами " * 2}
                for item in payload["cases"]
            ]
        }
        r = client.post("/api/skill-assessment/public/part2-cases?token=" + token, json=submit_answers)
        assert r.status_code == 200

        assert len(outbound.messages) >= 3
        text = outbound.messages[-1]["text"]
        assert "Нужно оценить сотрудника: Иванов Иван Иванович" in text
        assert "Должность: менеджер по продажам" in text
        assert "Подразделение: Отдел продаж" in text
        assert "Этап: оценка руководителем" in text
        assert "Дедлайн оценки:" in text
        assert "KPI:" in text
        assert "- Точность бухгалтерского учета" in text
        assert "- Своевременное закрытие периода" in text
        assert "Открыть страницу: https://example.test/api/skill-assessment/ui/manager-assessment?token=" in text


def test_complete_part2_sends_employee_protocol_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
    from skill_assessment.runner import app

    monkeypatch.setenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND", "mock")
    monkeypatch.setenv("SKILL_ASSESSMENT_PUBLIC_BASE_URL", "https://example.test")
    outbound = get_telegram_outbound()
    assert outbound.__class__.__name__ == "FakeTelegramOutbound"
    outbound.clear()
    client_id = "c_tg_proto_" + uuid.uuid4().hex[:8]
    employee_id = "e_tg_proto_" + uuid.uuid4().hex[:8]

    with TestClient(app) as client:
        r = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": employee_id})
        assert r.status_code == 200
        session_id = r.json()["id"]

        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200
        token = r.json().get("part1_docs_checklist_token")
        assert token and len(token) > 16

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist")
        assert r.status_code == 200
        answers = {q["id"]: "partial" for q in r.json()["questions"]}
        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist",
            json={"answers": answers, "complete": True},
        )
        assert r.status_code == 200

        r = client.get("/api/skill-assessment/public/part2-cases?token=" + token)
        assert r.status_code == 200
        payload = r.json()
        submit_answers = {
            "answers": [
                {"case_id": item["case_id"], "answer": "Развернутый ответ по кейсу с рисками, KPI, шагами и регламентами " * 2}
                for item in payload["cases"]
            ]
        }
        r = client.post("/api/skill-assessment/public/part2-cases?token=" + token, json=submit_answers)
        assert r.status_code == 200

        employee_msgs = [m["text"] for m in outbound.messages if "Оценка по кейсам завершена" in m["text"]]
        assert employee_msgs
        assert "добавлена в общий протокол" in employee_msgs[-1]
        assert "https://example.test/api/skill-assessment/public/report/html?token=" in employee_msgs[-1]


def test_complete_manager_assessment_sends_employee_updated_protocol_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
    from skill_assessment.integration.hr_core import EmployeeSnapshot
    from skill_assessment.runner import app
    from skill_assessment.services import manager_assessment as manager_assessment_svc

    monkeypatch.setenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND", "mock")
    monkeypatch.setenv("TELEGRAM_EXAM_MANAGER_CHAT_ID", "mgr_chat_001")
    monkeypatch.setenv("SKILL_ASSESSMENT_PUBLIC_BASE_URL", "https://example.test")

    def _fake_get_employee(db, client_id, employee_id):
        return EmployeeSnapshot(
            id=str(employee_id or "emp"),
            client_id=str(client_id),
            display_name="Иванов Иван Иванович",
            position_label="менеджер по продажам",
            position_code="SALES_MANAGER",
            department_code="SALES",
        )

    monkeypatch.setattr(manager_assessment_svc, "get_employee", _fake_get_employee)

    outbound = get_telegram_outbound()
    assert outbound.__class__.__name__ == "FakeTelegramOutbound"
    outbound.clear()
    client_id = "c_tg_mgr_done_" + uuid.uuid4().hex[:8]
    employee_id = "e_tg_mgr_done_" + uuid.uuid4().hex[:8]

    with TestClient(app) as client:
        r = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": employee_id})
        assert r.status_code == 200
        session_id = r.json()["id"]

        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200
        token = r.json().get("part1_docs_checklist_token")
        assert token and len(token) > 16

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist")
        assert r.status_code == 200
        answers = {q["id"]: "partial" for q in r.json()["questions"]}
        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist",
            json={"answers": answers, "complete": True},
        )
        assert r.status_code == 200

        r = client.get("/api/skill-assessment/public/part2-cases?token=" + token)
        assert r.status_code == 200
        payload = r.json()
        submit_answers = {
            "answers": [
                {"case_id": item["case_id"], "answer": "Развернутый ответ по кейсу с KPI, шагами, рисками и регламентами " * 2}
                for item in payload["cases"]
            ]
        }
        r = client.post("/api/skill-assessment/public/part2-cases?token=" + token, json=submit_answers)
        assert r.status_code == 200

        r = client.get(f"/api/skill-assessment/sessions/{session_id}")
        assert r.status_code == 200
        manager_token = r.json().get("manager_assessment_token")
        assert manager_token and len(manager_token) > 16

        r = client.get("/api/skill-assessment/public/manager-assessment?token=" + manager_token)
        assert r.status_code == 200
        ratings = [{"skill_id": item["skill_id"], "level": 3} for item in r.json()["skills"]]
        r = client.post(
            "/api/skill-assessment/public/manager-assessment?token=" + manager_token,
            json={"ratings": ratings},
        )
        assert r.status_code == 200

        employee_msgs = [m["text"] for m in outbound.messages if "Оценка руководителя добавлена в общий протокол" in m["text"]]
        assert employee_msgs
        assert "опрос, кейсы и оценка руководителя" in employee_msgs[-1]
        assert "https://example.test/api/skill-assessment/public/report/html?token=" in employee_msgs[-1]


def test_post_phase_part1_to_part2_sends_part2_case_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """HR «Перейти к кейсу»: POST /sessions/.../phase part2 шлёт в Telegram уведомление и тексты кейсов (и опционально веб-ссылку)."""
    from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
    from skill_assessment.runner import app

    monkeypatch.setenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND", "mock")
    # Произвольный chat_id только для изоляции теста (не совпадает с реальными людьми и не из прод-.env).
    monkeypatch.setenv("TELEGRAM_DOCS_SURVEY_FALLBACK_CHAT_ID", "900000991")
    monkeypatch.setenv("SKILL_ASSESSMENT_PUBLIC_BASE_URL", "https://example.test")

    outbound = get_telegram_outbound()
    assert outbound.__class__.__name__ == "FakeTelegramOutbound"
    outbound.clear()
    client_id = "c_phase_p2_" + uuid.uuid4().hex[:8]
    employee_id = "e_phase_p2_" + uuid.uuid4().hex[:8]

    with TestClient(app) as client:
        r = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": employee_id})
        assert r.status_code == 200
        session_id = r.json()["id"]
        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200
        outbound.clear()

        r = client.post(f"/api/skill-assessment/sessions/{session_id}/phase", json={"phase": "part2"})
        assert r.status_code == 200
        assert r.json()["phase"] == "part2"
        assert len(outbound.messages) >= 1
        combined = "\n".join(m["text"] for m in outbound.messages)
        assert "Кейс 1 из" in combined
        assert "Навыки в фокусе" not in combined
        assert "основной навык" not in combined.lower()
        assert "part2-case?token=" not in combined


def test_examination_complete_advances_linked_assessment_and_sends_part2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Завершение экзамена по регламентам переводит сессию оценки из part1 в part2 и шлёт то же уведомление, что и чек-лист."""
    from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
    from skill_assessment.runner import app

    monkeypatch.setenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND", "mock")
    monkeypatch.setenv("TELEGRAM_DOCS_SURVEY_FALLBACK_CHAT_ID", "900000992")
    monkeypatch.setenv("SKILL_ASSESSMENT_PUBLIC_BASE_URL", "https://example.test")

    outbound = get_telegram_outbound()
    assert outbound.__class__.__name__ == "FakeTelegramOutbound"
    outbound.clear()
    client_id = "c_ex_sa_" + uuid.uuid4().hex[:8]
    employee_id = "e_ex_sa_" + uuid.uuid4().hex[:8]

    with TestClient(app) as client:
        r = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": employee_id})
        assert r.status_code == 200
        sa_id = r.json()["id"]
        r = client.post(f"/api/skill-assessment/sessions/{sa_id}/start")
        assert r.status_code == 200
        assert r.json()["phase"] == "part1"
        outbound.clear()

        r = client.post(
            "/api/skill-assessment/examination/sessions",
            json={"client_id": client_id, "employee_id": employee_id, "scenario_id": "regulation_v1"},
        )
        assert r.status_code == 200
        sid = r.json()["id"]

        r = client.post(f"/api/skill-assessment/examination/sessions/{sid}/consent", json={"accepted": True})
        assert r.status_code == 200
        r = client.post(f"/api/skill-assessment/examination/sessions/{sid}/intro/done")
        assert r.status_code == 200

        for i in range(5):
            r = client.post(
                f"/api/skill-assessment/examination/sessions/{sid}/answer",
                json={"transcript_text": f"Ответ экзамен {i}"},
            )
            assert r.status_code == 200
        assert r.json()["phase"] == "protocol"

        r = client.post(f"/api/skill-assessment/examination/sessions/{sid}/complete")
        assert r.status_code == 200
        assert r.json()["phase"] == "completed"

        r = client.get(f"/api/skill-assessment/sessions/{sa_id}")
        assert r.status_code == 200
        assert r.json()["phase"] == "part2"

        assert len(outbound.messages) >= 1
        combined = "\n".join(m["text"] for m in outbound.messages)
        cl = combined.lower()
        assert ("этап 2" in cl) or ("часть 2" in cl)
        assert "навыки в фокусе" not in cl
        assert "основной навык" not in cl
        assert "part2-case?token=" not in cl
