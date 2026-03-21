"""Сквозной сценарий: таксономия → сессия → результат.

Запуск из корня typical_infrastructure::

    pytest path\\to\\skill_assessment\\tests\\test_assessment_flow.py -q
"""

from __future__ import annotations

from fastapi.testclient import TestClient


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
        assert len(skills) >= 1
        skill_id = skills[0]["id"]

        r = client.post("/api/skill-assessment/sessions", json={"client_id": "test_client_01"})
        assert r.status_code == 200
        session_id = r.json()["id"]

        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        assert r.status_code == 200
        assert r.json()["status"] == "in_progress"

        r = client.post(
            f"/api/skill-assessment/sessions/{session_id}/results",
            json={"skill_id": skill_id, "level": 2, "evidence_notes": {"case": "Ответил по кейсу"}},
        )
        assert r.status_code == 200
        assert r.json()["level"] == 2

        r = client.get(f"/api/skill-assessment/sessions/{session_id}/results")
        assert r.status_code == 200
        assert len(r.json()) == 1

        r = client.post(f"/api/skill-assessment/sessions/{session_id}/complete")
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
