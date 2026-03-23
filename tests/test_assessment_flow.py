# route: (pytest) | file: tests/test_assessment_flow.py
"""Сквозной сценарий: таксономия → сессия → результат.

Запуск из корня typical_infrastructure::

    pytest path\\to\\skill_assessment\\tests\\test_assessment_flow.py -q
"""

from __future__ import annotations

import uuid
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook


def test_taxonomy_sessions_results_flow() -> None:
    from skill_assessment.runner import app

    with TestClient(app) as client:
        r = client.get("/api/skill-assessment/taxonomy/domains")
        assert r.status_code == 200
        domains = r.json()
        assert len(domains) >= 1
        domain_id = domains[0]["id"]

        r = client.get(f"/api/skill-assessment/taxonomy/skills?domain_id={domain_id}")
        assert r.status_code == 200
        skills = r.json()
        assert len(skills) >= 2
        skill_id = skills[0]["id"]
        skill2_id = skills[1]["id"]

        r = client.post("/api/skill-assessment/sessions", json={"client_id": "test_client_01"})
        assert r.status_code == 200
        session_id = r.json()["id"]
        assert r.json()["phase"] == "draft"

        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200
        started = r.json()
        assert started["status"] == "in_progress"
        assert started["phase"] == "part1"
        assert "docs_survey_telegram" in started

        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part1/turns",
            json={
                "turns": [
                    {"role": "llm", "text": "Какой KPI главный?"},
                    {"role": "user", "text": "Конверсия из лида в сделку."},
                ]
            },
        )
        assert r.status_code == 200
        assert len(r.json()) == 2
        assert r.json()[0]["role"] == "llm"
        r = client.get(f"/api/skill-assessment/sessions/{session_id}")
        assert r.json()["phase"] == "part1"

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/case?skill_id={skill_id}")
        assert r.status_code == 200
        case = r.json()
        assert case["source"] == "template"
        assert skill_id == case["skill_id"]
        assert len(case["text"]) > 20

        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/manager-ratings",
            json={"ratings": [{"skill_id": skill_id, "level": 2}]},
        )
        assert r.status_code == 200
        assert r.json()[0]["level"] == 2
        assert r.json()[0]["evidence_notes"].get("manager")

        r = client.get(f"/api/skill-assessment/sessions/{session_id}")
        assert r.json()["phase"] == "part3"

        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/results",
            json={"skill_id": skill2_id, "level": 2, "evidence_notes": {"case": "Ответил по кейсу"}},
        )
        assert r.status_code == 200
        assert r.json()["level"] == 2

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/results")
        assert r.status_code == 200
        assert len(r.json()) == 2

        r = client.post(f"/api/skill-assessment/sessions/{session_id}/complete")
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
        assert r.json()["phase"] == "completed"

        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/phase",
            json={"phase": "report"},
        )
        assert r.status_code == 200
        assert r.json()["phase"] == "report"

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/report")
        assert r.status_code == 200
        rep = r.json()
        assert rep["session"]["id"] == session_id
        assert len(rep["rows"]) >= 1
        assert rep["rows"][0]["skill_title"]
        assert len(rep["part1_turns"]) == 2
        assert rep["part1_turns"][0]["text"] == "Какой KPI главный?"

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/report/html")
        assert r.status_code == 200
        assert "Отчёт" in r.text
        assert "Конверсия" in r.text


def test_sessions_list_filters_pagination_and_part1_fields() -> None:
    """GET /sessions: items+total, фильтры, поля Part 1 в ответе."""
    from skill_assessment.runner import app

    with TestClient(app) as client:
        r = client.post(
            "/api/skill-assessment/sessions",
            json={"client_id": "hist_client", "employee_id": "emp_hist_1"},
        )
        assert r.status_code == 200
        sid = r.json()["id"]

        r = client.get(
            "/api/skill-assessment/sessions",
            params={"client_id": "hist_client", "limit": 20, "offset": 0},
        )
        assert r.status_code == 200
        data = r.json()
        assert "items" in data and "total" in data
        assert isinstance(data["items"], list)
        assert data["total"] >= 1
        assert any(x.get("id") == sid for x in data["items"])
        row = next(x for x in data["items"] if x["id"] == sid)
        assert "docs_survey_pd_consent_status" in row
        assert "docs_survey_pd_consent_at" in row
        assert "docs_survey_scheduled_at" in row
        assert "docs_survey_readiness_answer" in row

        r = client.get(
            "/api/skill-assessment/sessions",
            params={
                "client_id": "hist_client",
                "employee_id": "emp_hist_1",
                "phase": "draft",
                "limit": 5,
            },
        )
        assert r.status_code == 200
        assert r.json()["total"] >= 1


def test_sessions_list_employee_id_case_insensitive() -> None:
    """Фильтр employee_id совпадает с БД без учёта регистра UUID."""
    from skill_assessment.runner import app

    eid_lower = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with TestClient(app) as client:
        r = client.post(
            "/api/skill-assessment/sessions",
            json={"client_id": "c_case_eid", "employee_id": eid_lower},
        )
        assert r.status_code == 200
        r = client.get(
            "/api/skill-assessment/sessions",
            params={
                "client_id": "c_case_eid",
                "employee_id": eid_lower.upper(),
                "limit": 20,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 1
        assert any(x.get("employee_id", "").lower() == eid_lower for x in data["items"])


def test_session_cancel() -> None:
    """Отмена незавершённого назначения; повторная отмена — 400."""
    from skill_assessment.runner import app

    with TestClient(app) as client:
        r = client.post(
            "/api/skill-assessment/sessions",
            json={"client_id": "c_cancel", "employee_id": "e_cancel"},
        )
        assert r.status_code == 200
        sid = r.json()["id"]

        r = client.post(f"/api/skill-assessment/sessions/{sid}/cancel", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

        r = client.post(f"/api/skill-assessment/sessions/{sid}/cancel", json={})
        assert r.status_code == 400


def test_classifier_import_and_report() -> None:
    from skill_assessment.runner import app

    with TestClient(app) as client:
        sid = f"IMP_{uuid.uuid4().hex[:10]}"
        buf = BytesIO()
        wb = Workbook()
        ws = wb.active
        ws.title = "Классификатор_навыков"
        ws.append(["skill_id", "department", "domain", "skill_name", "source", "note"])
        ws.append([sid, "Управление", "Импорт-тест", "Навык импорта", "test", ""])
        wb.save(buf)
        buf.seek(0)
        r = client.post(
            "/api/skill-assessment/taxonomy/import-classifier",
            files={
                "file": (
                    "classifier.xlsx",
                    buf.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert r.status_code == 200
        assert r.json()["skills_created"] + r.json()["skills_updated"] >= 1

        r = client.get("/api/skill-assessment/taxonomy/skills")
        assert r.status_code == 200
        skills = r.json()
        imp = next((s for s in skills if s["code"] == sid), None)
        assert imp is not None

        r = client.post("/api/skill-assessment/sessions", json={"client_id": "c_import"})
        session_id = r.json()["id"]
        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200
        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/manager-ratings",
            json={"ratings": [{"skill_id": imp["id"], "level": 2}]},
        )
        assert r.status_code == 200

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/report")
        assert r.status_code == 200
        assert r.json()["rows"][0]["skill_code"] == sid
