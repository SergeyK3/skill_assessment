from __future__ import annotations

from skill_assessment.services import examination_question_plan as qp


def test_compose_examination_question_plan_varies_by_seed_key(monkeypatch) -> None:
    monkeypatch.setattr(
        qp,
        "get_examination_question_texts",
        lambda db, client_id, employee_id: [
            "Регламент: как выполняете план продаж в воронке?",
            "Регламент: как работаете с возражениями?",
            "Регламент: как фиксируете договоренности в CRM?",
        ],
    )
    monkeypatch.setattr(qp, "get_examination_instructions_folder_url", lambda db, client_id, employee_id: "")

    q1 = qp.compose_examination_question_plan(object(), "c1", "e1", seed_key="session-A")
    q2 = qp.compose_examination_question_plan(object(), "c1", "e1", seed_key="session-B")

    assert q1 is not None and q2 is not None
    assert len(q1) >= 5 and len(q2) >= 5
    assert q1 != q2
