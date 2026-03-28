# route: (pytest) | file: tests/test_assessment_flow.py
"""Сквозной сценарий: таксономия → сессия → результат.

Запуск из корня typical_infrastructure::

    pytest path\\to\\skill_assessment\\tests\\test_assessment_flow.py -q
"""

from __future__ import annotations

import json
import uuid
from io import BytesIO

import pytest
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
        assert case["source"] in {"template", "llm"}
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
        assert rep["part1_overall_level"] is not None
        assert rep["development_recommendations"]

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/report/html")
        assert r.status_code == 200
        assert "Отчёт" in r.text
        assert "Конверсия" in r.text
        assert "Рекомендации по развитию" in r.text


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
        assert "part1_docs_checklist_completed" in row

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


def test_create_session_reuses_existing_active_session() -> None:
    """Один сотрудник -> одна активная сессия на весь цикл; повторный POST /sessions не плодит дубль."""
    from skill_assessment.runner import app

    with TestClient(app) as client:
        suffix = uuid.uuid4().hex[:8]
        client_id = "c_reuse_" + suffix
        employee_id = "e_reuse_" + suffix
        r1 = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": employee_id})
        assert r1.status_code == 200
        first = r1.json()
        assert first["status"] == "draft"

        r2 = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": employee_id})
        assert r2.status_code == 200
        second = r2.json()
        assert second["id"] == first["id"]
        assert second["status"] == "draft"

        r3 = client.post(f"/api/skill-assessment/sessions/{first['id']}/start")
        assert r3.status_code == 200
        assert r3.json()["status"] == "in_progress"

        r4 = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": employee_id})
        assert r4.status_code == 200
        third = r4.json()
        assert third["id"] == first["id"]
        assert third["status"] == "in_progress"


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


def test_part1_docs_checklist_save_and_complete() -> None:
    """GET/POST part1/docs-checklist: черновик, завершение, part1→part2, флаг в сессии."""
    from skill_assessment.runner import app

    with TestClient(app) as client:
        suffix = uuid.uuid4().hex[:8]
        r = client.post(
            "/api/skill-assessment/sessions",
            json={"client_id": "c_docs_chk_" + suffix, "employee_id": "e_docs_chk_" + suffix},
        )
        assert r.status_code == 200
        session_id = r.json()["id"]

        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200
        assert r.json()["phase"] == "part1"
        assert r.json().get("part1_docs_checklist_completed") is False

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist")
        assert r.status_code == 200
        payload = r.json()
        assert payload["completed"] is False
        assert len(payload["questions"]) >= 5

        qids = [q["id"] for q in payload["questions"]]
        answers = {qid: "yes" for qid in qids}
        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist",
            json={"answers": answers, "complete": False},
        )
        assert r.status_code == 200
        assert r.json()["completed"] is False

        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist",
            json={"answers": answers, "complete": True},
        )
        assert r.status_code == 200
        done = r.json()
        assert done["completed"] is True
        assert done["phase"] == "part2"

        r = client.get(f"/api/skill-assessment/sessions/{session_id}")
        assert r.status_code == 200
        assert r.json()["phase"] == "part2"
        assert r.json().get("part1_docs_checklist_completed") is True


def test_public_part1_docs_checklist_by_token() -> None:
    """Публичный GET/POST по token без cookie HR."""
    from skill_assessment.runner import app

    with TestClient(app) as client:
        suffix = uuid.uuid4().hex[:8]
        r = client.post(
            "/api/skill-assessment/sessions",
            json={"client_id": "c_pub_docs_" + suffix, "employee_id": "e_pub_docs_" + suffix},
        )
        assert r.status_code == 200
        session_id = r.json()["id"]
        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200

        r = client.get(f"/api/skill-assessment/sessions/{session_id}")
        assert r.status_code == 200
        tok = r.json().get("part1_docs_checklist_token")
        assert tok and len(tok) > 16

        r = client.get("/api/skill-assessment/public/part1-docs-checklist?token=" + tok)
        assert r.status_code == 200
        payload = r.json()
        qids = [q["id"] for q in payload["questions"]]
        answers = {qid: "partial" for qid in qids}

        r = client.post(
            "/api/skill-assessment/public/part1-docs-checklist?token=" + tok,
            json={"answers": answers, "complete": True},
        )
        assert r.status_code == 200
        assert r.json()["completed"] is True

        r = client.get("/api/skill-assessment/public/part1-docs-checklist?token=invalid_token_xxxxxxxxxxxx")
        assert r.status_code == 404


def test_public_part2_case_by_token_after_part1_complete() -> None:
    """После завершения Part 1 сотрудник может открыть кейс по тому же токену."""
    from skill_assessment.runner import app

    with TestClient(app) as client:
        suffix = uuid.uuid4().hex[:8]
        r = client.post(
            "/api/skill-assessment/sessions",
            json={"client_id": "c_pub_case_" + suffix, "employee_id": "e_pub_case_" + suffix},
        )
        assert r.status_code == 200
        session_id = r.json()["id"]

        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200
        token = r.json().get("part1_docs_checklist_token")
        assert token and len(token) > 16

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist")
        assert r.status_code == 200
        qids = [q["id"] for q in r.json()["questions"]]
        answers = {qid: "partial" for qid in qids}

        r = client.post(
            "/api/skill-assessment/public/part1-docs-checklist?token=" + token,
            json={"answers": answers, "complete": True},
        )
        assert r.status_code == 200
        assert r.json()["phase"] == "part2"

        r = client.get("/api/skill-assessment/public/part2-case?token=" + token)
        assert r.status_code == 200
        payload = r.json()
        assert payload["session_id"] == session_id
        assert payload["source"] in {"template", "llm"}
        assert len(payload["text"]) > 40


def test_public_part2_cases_submit_and_score_scale() -> None:
    """Part 2: сотрудник получает несколько кейсов, отправляет ответы, ИИ-оценка идёт по шкале блока кейсов."""
    from skill_assessment.runner import app

    with TestClient(app) as client:
        suffix = uuid.uuid4().hex[:8]
        r = client.post(
            "/api/skill-assessment/sessions",
            json={"client_id": "c_part2_bundle_" + suffix, "employee_id": "e_part2_bundle_" + suffix},
        )
        assert r.status_code == 200
        session_id = r.json()["id"]

        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200
        token = r.json().get("part1_docs_checklist_token")
        assert token and len(token) > 16

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist")
        assert r.status_code == 200
        qids = [q["id"] for q in r.json()["questions"]]
        answers = {qid: "partial" for qid in qids}
        r = client.post(
            "/api/skill-assessment/public/part1-docs-checklist?token=" + token,
            json={"answers": answers, "complete": True},
        )
        assert r.status_code == 200

        r = client.get("/api/skill-assessment/public/part2-cases?token=" + token)
        assert r.status_code == 200
        payload = r.json()
        assert payload["case_count"] == 2
        assert payload["allotted_minutes"] == 20
        assert len(payload["cases"]) == 2
        assert len(payload["covered_skills"]) >= 2
        assert payload["remaining_skills"] == []
        assert payload.get("ai_commission_consensus") is None
        assert "llm_costs" not in payload
        for item in payload["cases"]:
            assert len(item["covered_skills"]) >= 1
            assert item.get("skill_evaluations") == []

        submit_answers = {
            "answers": [
                {
                    "case_id": payload["cases"][0]["case_id"],
                    "answer": "Подробно описываю решение кейса с шагами, согласованиями, рисками, регламентами, KPI и проверкой результата. "
                    "Здесь достаточно текста, чтобы кейс считался решённым на базовом уровне.",
                },
                {
                    "case_id": payload["cases"][1]["case_id"],
                    "answer": "Короткий ответ.",
                },
            ]
        }
        r = client.post("/api/skill-assessment/public/part2-cases?token=" + token, json=submit_answers)
        assert r.status_code == 200
        done = r.json()
        assert done["completed"] is True
        assert done["solved_cases"] == 1
        assert done["overall_pct"] == 67
        assert len(done["cases"]) == 2
        assert done["cases"][0]["answer"]
        assert done["cases"][0]["evaluation_note"]
        assert done["cases"][0]["case_level_0_3"] is not None
        assert done["cases"][0]["case_pct_0_100"] is not None
        assert done["cases"][0]["skill_evaluations"]
        assert done["ai_commission_consensus"]
        assert "llm_costs" not in done

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/part2-cases")
        assert r.status_code == 200
        hr_cases = r.json()
        assert hr_cases["ai_commission_consensus"]
        assert "llm_costs" in hr_cases

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/report")
        assert r.status_code == 200
        rep = r.json()
        assert rep["part2_case_count"] == 2
        assert rep["part2_solved_cases"] == 1
        assert rep["part2_overall_pct"] == 67
        assert len(rep["part2_cases"]) == 2
        assert rep["part2_cases"][0]["text"]
        assert rep["part2_cases"][0]["answer"]
        assert rep["part2_cases"][0]["covered_skills"]
        assert rep["part2_cases"][0]["skill_evaluations"]
        assert rep["part2_ai_commission_consensus"]
        assert "part2_llm_costs" in rep
        assert "hr_no_show" in rep["session"]
        assert "part1_docs_checklist_token" in rep["session"]

        r = client.get("/api/skill-assessment/public/report?token=" + token)
        assert r.status_code == 200
        pub_rep = r.json()
        assert pub_rep["session"]["id"] == session_id
        assert pub_rep["part2_overall_pct"] == 67
        assert pub_rep["part2_ai_commission_consensus"]
        assert "part2_llm_costs" not in pub_rep
        assert "hr_no_show" not in pub_rep["session"]
        assert "part1_docs_checklist_token" not in pub_rep["session"]
        assert "manager_assessment_token" not in pub_rep["session"]

        r = client.get("/api/skill-assessment/public/report/html?token=" + token)
        assert r.status_code == 200
        assert "Часть 2 — кейсы" in r.text
        assert "Решение сотрудника" in r.text
        assert "Стоимость LLM-вызовов" not in r.text

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/report/html")
        assert r.status_code == 200
        assert "Стоимость LLM-вызовов" in r.text
        assert "Входящие токены" in r.text
        assert "Исходящие токены" in r.text
        assert "USD" in r.text
        assert "RUB" in r.text

        r = client.get(f"/api/skill-assessment/sessions/{session_id}")
        assert r.status_code == 200
        assert r.json()["phase"] == "part3"


def test_session_can_offer_additional_cases_for_uncovered_skills() -> None:
    """HR может дозапросить дополнительные кейсы по навыкам, которые ещё не покрыты текущим набором."""
    import json

    from skill_assessment.runner import app
    from app.db import SessionLocal
    from skill_assessment.infrastructure.db_models import AssessmentSessionRow

    with TestClient(app) as client:
        suffix = uuid.uuid4().hex[:8]
        r = client.post(
            "/api/skill-assessment/sessions",
            json={"client_id": "c_part2_more_" + suffix, "employee_id": "e_part2_more_" + suffix},
        )
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
            "/api/skill-assessment/public/part1-docs-checklist?token=" + token,
            json={"answers": answers, "complete": True},
        )
        assert r.status_code == 200

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/part2-cases")
        assert r.status_code == 200
        initial = r.json()
        assert initial["remaining_skills"] == []

        db = SessionLocal()
        try:
            row = db.get(AssessmentSessionRow, session_id)
            assert row is not None
            payload = json.loads(row.part2_cases_json or "{}")
            assert payload.get("cases")
            first_case = payload["cases"][0]
            keep_only = first_case.get("covered_skills")[:1]
            first_case["covered_skills"] = keep_only
            payload["cases"] = [first_case]
            payload["case_count"] = 1
            payload["allotted_minutes"] = 10
            row.part2_cases_json = json.dumps(payload, ensure_ascii=False)
            db.commit()
        finally:
            db.close()

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/part2-cases")
        assert r.status_code == 200
        degraded = r.json()
        assert len(degraded["remaining_skills"]) >= 1
        remaining_id = degraded["remaining_skills"][0]["skill_id"]

        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part2-cases/additional",
            json={"skill_ids": [remaining_id]},
        )
        assert r.status_code == 200
        expanded = r.json()
        assert expanded["case_count"] >= 2
        assert any(
            remaining_id in [s["skill_id"] for s in item["covered_skills"]]
            for item in expanded["cases"]
        )


def test_public_manager_assessment_by_token_after_part2_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """После кейсов руководитель получает отдельную публичную страницу Part 3 по своему токену."""
    from skill_assessment.runner import app
    from app.db import SessionLocal
    from sqlalchemy import select
    from skill_assessment.infrastructure.db_models import CompetencyCatalogVersionRow, CompetencyMatrixRow
    from skill_assessment.integration.hr_core import EmployeeSnapshot
    from skill_assessment.services import manager_assessment as manager_assessment_svc

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

    inactive_row_id: str | None = None
    with SessionLocal() as db:
        version_id = db.scalar(
            select(CompetencyCatalogVersionRow.id)
            .where(
                CompetencyCatalogVersionRow.client_id.is_(None),
                CompetencyCatalogVersionRow.status == "active",
            )
            .order_by(CompetencyCatalogVersionRow.created_at.desc())
            .limit(1)
        )
        assert version_id
        inactive_row = db.scalar(
            select(CompetencyMatrixRow)
            .where(
                CompetencyMatrixRow.version_id == version_id,
                CompetencyMatrixRow.position_code == "SALES_MANAGER",
                CompetencyMatrixRow.department_code == "SALES",
            )
            .order_by(CompetencyMatrixRow.skill_rank.asc())
            .limit(1)
        )
        assert inactive_row is not None
        inactive_row_id = inactive_row.id
        inactive_row.is_active = False
        db.commit()

    try:
        with TestClient(app) as client:
            suffix = uuid.uuid4().hex[:8]
            r = client.post(
                "/api/skill-assessment/sessions",
                json={"client_id": "c_mgr_pub_" + suffix, "employee_id": "e_mgr_pub_" + suffix},
            )
            assert r.status_code == 200
            session_id = r.json()["id"]

            r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
            assert r.status_code == 200
            token = r.json().get("part1_docs_checklist_token")
            assert token and len(token) > 16

            r = client.get(f"/api/skill-assessment/sessions/{session_id}/part1/docs-checklist")
            assert r.status_code == 200
            qids = [q["id"] for q in r.json()["questions"]]
            answers = {qid: "partial" for qid in qids}
            r = client.post(
                "/api/skill-assessment/public/part1-docs-checklist?token=" + token,
                json={"answers": answers, "complete": True},
            )
            assert r.status_code == 200

            r = client.get("/api/skill-assessment/public/part2-cases?token=" + token)
            assert r.status_code == 200
            payload = r.json()
            submit_answers = {
                "answers": [
                    {"case_id": item["case_id"], "answer": "Развернутый ответ по кейсу с шагами, рисками и KPI " * 2}
                    for item in payload["cases"]
                ]
            }
            r = client.post("/api/skill-assessment/public/part2-cases?token=" + token, json=submit_answers)
            assert r.status_code == 200

            r = client.get(f"/api/skill-assessment/sessions/{session_id}")
            assert r.status_code == 200
            session = r.json()
            manager_token = session.get("manager_assessment_token")
            assert manager_token and len(manager_token) > 16
            assert session.get("manager_assessment_deadline_label")

            r = client.get("/api/skill-assessment/public/manager-assessment?token=" + manager_token)
            assert r.status_code == 200
            page = r.json()
            assert page["session_id"] == session_id
            assert page["stage_title"] == "оценка руководителем"
            assert page["can_submit"] is True
            assert len(page["skills"]) >= 1
            assert any(item["is_active"] is False for item in page["skills"])
            active_skills = [item for item in page["skills"] if item["is_active"]]
            assert active_skills
            assert "KPI" in (page["kpi_summary"] or "")

            ratings = [
                {
                    "skill_id": item["skill_id"],
                    "level": 3,
                    "comment": "Тестовый комментарий руководителя по навыку для отчёта.",
                }
                for item in active_skills
            ]
            r = client.post(
                "/api/skill-assessment/public/manager-assessment?token=" + manager_token,
                json={"ratings": ratings},
            )
            assert r.status_code == 200
            saved = r.json()
            assert saved["saved_count"] == len(active_skills)
            assert saved["status"] == "completed"
            assert saved["phase"] == "completed"

            r = client.get(f"/api/skill-assessment/sessions/{session_id}/report")
            assert r.status_code == 200
            rep = r.json()
            assert any(
                row.get("evidence_manager") == "Тестовый комментарий руководителя по навыку для отчёта."
                for row in rep.get("rows", [])
            )
            assert "Оценка руководителя" not in json.dumps(rep, ensure_ascii=False)

            r = client.get(f"/api/skill-assessment/sessions/{session_id}")
            assert r.status_code == 200
            assert r.json()["status"] == "completed"
    finally:
        if inactive_row_id:
            with SessionLocal() as db:
                row = db.get(CompetencyMatrixRow, inactive_row_id)
                if row is not None:
                    row.is_active = True
                    db.commit()


def test_part2_payload_normalizes_legacy_shape() -> None:
    """Старый part2_cases_json автоматически дополняется новыми полями и остаётся читаемым."""
    import json

    from app.db import SessionLocal
    from skill_assessment.infrastructure.db_models import AssessmentSessionRow
    from skill_assessment.runner import app

    with TestClient(app) as client:
        suffix = uuid.uuid4().hex[:8]
        r = client.post(
            "/api/skill-assessment/sessions",
            json={"client_id": "c_part2_legacy_" + suffix, "employee_id": "e_part2_legacy_" + suffix},
        )
        assert r.status_code == 200
        session_id = r.json()["id"]

        db = SessionLocal()
        try:
            row = db.get(AssessmentSessionRow, session_id)
            assert row is not None
            row.part2_cases_json = json.dumps(
                {
                    "case_count": 1,
                    "allotted_minutes": 10,
                    "completed": True,
                    "completed_at": "2026-03-28T10:00:00+00:00",
                    "solved_cases": 1,
                    "overall_pct": 100,
                    "cases": [
                        {
                            "case_id": "legacy_case_1",
                            "skill_id": "legacy_skill_id",
                            "skill_code": "LEGACY",
                            "skill_title": "Legacy skill",
                            "text": "Legacy case text",
                            "source": "template",
                            "answer": "Legacy answer",
                            "passed": True,
                            "evaluation_note": "Legacy note",
                        }
                    ],
                },
                ensure_ascii=False,
            )
            db.commit()
        finally:
            db.close()

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/part2-cases")
        assert r.status_code == 200
        payload = r.json()
        assert payload["cases"][0]["covered_skills"]
        assert payload["cases"][0]["case_level_0_3"] is not None
        assert payload["cases"][0]["case_pct_0_100"] is not None
        assert payload["cases"][0]["skill_evaluations"] == []
        assert payload["ai_commission_consensus"]
        assert payload["llm_costs"]["steps"] == []


def test_part1_audio_stt_mock_and_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST …/part1/audio: mock STT → user-реплика и строка в отчёте."""
    monkeypatch.setenv("SKILL_ASSESSMENT_STT_PROVIDER", "mock")
    from sqlalchemy import delete

    from skill_assessment.runner import app
    from app.db import SessionLocal
    from skill_assessment.infrastructure.db_models import LlmPostSttBlacklistRow

    # Локальная SQLite может содержать паттерны из других прогонов (совпадают с mock STT).
    db = SessionLocal()
    try:
        db.execute(delete(LlmPostSttBlacklistRow))
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        r = client.post("/api/skill-assessment/sessions", json={"client_id": "c_part1_audio"})
        assert r.status_code == 200
        session_id = r.json()["id"]
        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200

        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part1/audio",
            files={"file": ("answer.webm", b"fake-audio-bytes", "audio/webm")},
        )
        assert r.status_code == 200
        turns = r.json()
        assert len(turns) == 1
        assert turns[0]["role"] == "user"
        assert "[STT mock]" in turns[0]["text"]

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/report")
        assert r.status_code == 200
        rep = r.json()
        assert any("[STT mock]" in t["text"] for t in rep["part1_turns"])
        assert rep["part1_overall_level"] is not None

        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part1/audio",
            files={"file": ("empty.webm", b"", "audio/webm")},
        )
        assert r.status_code == 400
        body = r.json()
        assert body.get("detail") == "part1_empty_audio" or body.get("error", {}).get("code") == "part1_empty_audio"


def test_llm_post_stt_blacklist_audio_and_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Чёрный список после STT: 422 при совпадении; POST part1/turns с role=user — та же проверка."""
    tag = f"blk_{uuid.uuid4().hex[:16]}"

    def _fake_transcribe(data: bytes, **kwargs: object) -> str:
        if not data:
            raise ValueError("empty_audio")
        return f"reply {tag} bytes={len(data)}"

    monkeypatch.setattr(
        "skill_assessment.services.stt_service.transcribe_audio_bytes",
        _fake_transcribe,
    )
    from skill_assessment.runner import app

    def _detail_code(payload: dict) -> str | None:
        d = payload.get("detail")
        if isinstance(d, dict):
            c = d.get("code")
            if c:
                return c
        err = payload.get("error")
        if isinstance(err, dict):
            return err.get("code")
        return None

    with TestClient(app) as client:
        r = client.post("/api/skill-assessment/sessions", json={"client_id": "c_bl_stt"})
        assert r.status_code == 200
        session_id = r.json()["id"]
        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200

        r = client.post(
            "/api/skill-assessment/admin/llm-post-stt-blacklist",
            json={"pattern": tag, "match_mode": "substring", "is_active": True},
        )
        assert r.status_code == 200
        bid = r.json()["id"]

        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part1/audio",
            files={"file": ("a.webm", b"x", "audio/webm")},
        )
        assert r.status_code == 422
        assert _detail_code(r.json()) == "llm_post_stt_blacklisted"

        r = client.patch(
            f"/api/skill-assessment/admin/llm-post-stt-blacklist/{bid}",
            json={"is_active": False},
        )
        assert r.status_code == 200
        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part1/audio",
            files={"file": ("b.webm", b"yy", "audio/webm")},
        )
        assert r.status_code == 200
        assert tag in r.json()[0]["text"]

        r = client.patch(
            f"/api/skill-assessment/admin/llm-post-stt-blacklist/{bid}",
            json={"is_active": True},
        )
        assert r.status_code == 200
        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part1/turns",
            json={"turns": [{"role": "user", "text": f"prefix {tag} suffix"}]},
        )
        assert r.status_code == 422
        assert _detail_code(r.json()) == "llm_post_stt_blacklisted"

        r = client.delete(f"/api/skill-assessment/admin/llm-post-stt-blacklist/{bid}")
        assert r.status_code == 200
        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/part1/turns",
            json={"turns": [{"role": "user", "text": f"prefix {tag} suffix"}]},
        )
        assert r.status_code == 200


def test_patch_docs_survey_slot_manual(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATCH …/docs-survey-slot: дата/время в локальной зоне → UTC в БД + поля для формы."""
    monkeypatch.setenv("DOCS_SURVEY_LOCAL_TIMEZONE", "Asia/Almaty")
    from skill_assessment.runner import app

    with TestClient(app) as client:
        r = client.post(
            "/api/skill-assessment/sessions",
            json={"client_id": "c_ds_slot", "employee_id": "e_ds_slot"},
        )
        assert r.status_code == 200
        sid = r.json()["id"]
        r = client.patch(
            f"/api/skill-assessment/sessions/{sid}/docs-survey-slot",
            json={"local_date": "2030-06-15", "local_time": "14:30"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body.get("docs_survey_slot_local_date") == "2030-06-15"
        assert body.get("docs_survey_slot_local_time") == "14:30"
        assert body.get("docs_survey_scheduled_at") is not None

        r = client.post(f"/api/skill-assessment/sessions/{sid}/cancel", json={})
        assert r.status_code == 200
        r = client.patch(
            f"/api/skill-assessment/sessions/{sid}/docs-survey-slot",
            json={"local_date": "2030-06-16", "local_time": "10:00"},
        )
        assert r.status_code == 400


def test_sessions_list_docs_survey_slot_filter_has_slot() -> None:
    """GET /sessions?docs_survey_slot_filter=has_slot — только с запланированным слотом."""
    from skill_assessment.runner import app

    with TestClient(app) as client:
        suffix = uuid.uuid4().hex[:8]
        client_id = "c_slot_f_" + suffix
        r = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": "e_slot_f_no_" + suffix})
        assert r.status_code == 200
        sid_no = r.json()["id"]
        r = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": "e_slot_f_yes_" + suffix})
        assert r.status_code == 200
        sid_yes = r.json()["id"]
        r = client.post(f"/api/skill-assessment/sessions/{sid_yes}/start")
        assert r.status_code == 200
        # Подставляем слот в БД (UTC naive), без Telegram
        from app.db import SessionLocal
        from skill_assessment.infrastructure.db_models import AssessmentSessionRow

        db = SessionLocal()
        try:
            row = db.get(AssessmentSessionRow, sid_yes)
            assert row is not None
            from datetime import datetime, timezone, timedelta

            row.docs_survey_scheduled_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
            db.commit()
        finally:
            db.close()

        r = client.get(
            "/api/skill-assessment/sessions",
            params={"client_id": client_id, "docs_survey_slot_filter": "has_slot"},
        )
        assert r.status_code == 200
        ids = [x["id"] for x in r.json()["items"]]
        assert sid_yes in ids
        assert sid_no not in ids


def test_delete_session_removes_row() -> None:
    """DELETE /sessions/{id} — запись исчезает из БД (HR)."""
    from fastapi.testclient import TestClient

    from skill_assessment.runner import app

    with TestClient(app) as client:
        r = client.post("/api/skill-assessment/sessions", json={"client_id": "c_del_test", "employee_id": "e_del"})
        assert r.status_code == 200
        sid = r.json()["id"]
        r = client.delete(f"/api/skill-assessment/sessions/{sid}")
        assert r.status_code == 204
        r = client.get(f"/api/skill-assessment/sessions/{sid}")
        assert r.status_code == 404
