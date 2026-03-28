# route: (pytest) | file: tests/test_examination_flow.py
"""Сессия экзамена regulation_v1: согласие → вступление → ответы → протокол → завершение."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_examination_regulation_v1_flow() -> None:
    from skill_assessment.runner import app

    with TestClient(app) as client:
        r = client.get("/api/skill-assessment/examination/scenarios/regulation_v1/questions")
        assert r.status_code == 200
        questions = r.json()
        assert len(questions) == 5
        assert questions[0]["seq"] == 0

        r = client.post(
            "/api/skill-assessment/examination/sessions",
            json={"client_id": "c1", "employee_id": "e1", "scenario_id": "regulation_v1"},
        )
        assert r.status_code == 200
        body = r.json()
        sid = body["id"]
        tok = body["access_token"]
        assert tok and len(tok) >= 16
        assert body["phase"] == "consent"
        assert body["question_count"] == 5

        r = client.get(f"/api/skill-assessment/examination/sessions/by-access-token/{tok}")
        assert r.status_code == 200
        assert r.json()["id"] == sid
        assert r.json()["access_token"] == tok

        r = client.post(f"/api/skill-assessment/examination/sessions/{sid}/consent", json={"accepted": False})
        assert r.status_code == 200
        assert r.json()["phase"] == "blocked_consent"

        r = client.post(f"/api/skill-assessment/examination/sessions/{sid}/consent", json={"accepted": True})
        assert r.status_code == 403

        r = client.post(f"/api/skill-assessment/examination/sessions/{sid}/hr/release-consent-block")
        assert r.status_code == 200
        assert r.json()["phase"] == "consent"

        r = client.post(f"/api/skill-assessment/examination/sessions/{sid}/consent", json={"accepted": True})
        assert r.status_code == 200
        assert r.json()["phase"] == "intro"

        r = client.post(f"/api/skill-assessment/examination/sessions/{sid}/intro/done")
        assert r.status_code == 200
        assert r.json()["phase"] == "questions"
        assert r.json()["current_question_index"] == 0

        r = client.get(f"/api/skill-assessment/examination/sessions/{sid}/current-question")
        assert r.status_code == 200
        assert r.json()["seq"] == 0

        for i in range(5):
            r = client.post(
                f"/api/skill-assessment/examination/sessions/{sid}/answer",
                json={"transcript_text": f"Ответ на вопрос {i}"},
            )
            assert r.status_code == 200
            if i < 4:
                assert r.json()["phase"] == "questions"
        assert r.json()["phase"] == "protocol"

        r = client.get(f"/api/skill-assessment/examination/sessions/{sid}/protocol")
        assert r.status_code == 200
        proto = r.json()
        assert len(proto["items"]) == 5
        assert "Ответ на вопрос 0" in proto["items"][0]["transcript_text"]
        assert proto["items"][0]["score_4"] >= 1
        assert proto["items"][0]["score_percent"] >= 50
        assert proto.get("average_score_4") is not None
        assert proto.get("employee_last_name") is not None
        assert proto.get("evaluated_at") is not None
        assert proto.get("evaluation_is_preliminary") is True

        r = client.post(f"/api/skill-assessment/examination/sessions/{sid}/complete")
        assert r.status_code == 200
        assert r.json()["phase"] == "completed"
        assert r.json()["status"] == "completed"

        r = client.get(f"/api/skill-assessment/examination/sessions?client_id=c1")
        assert r.status_code == 200
        assert any(x["id"] == sid for x in r.json())

        r = client.delete(f"/api/skill-assessment/examination/sessions/{sid}")
        assert r.status_code == 200
        r = client.get(f"/api/skill-assessment/examination/sessions/{sid}")
        assert r.status_code == 404


def test_examination_unsupported_scenario() -> None:
    from skill_assessment.runner import app

    with TestClient(app) as client:
        r = client.post(
            "/api/skill-assessment/examination/sessions",
            json={"client_id": "c2", "employee_id": "e2", "scenario_id": "unknown_v99"},
        )
        assert r.status_code == 400
        body = r.json()
        assert "unsupported_scenario_mvp" in str(body)
