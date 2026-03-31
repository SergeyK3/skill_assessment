from __future__ import annotations

from types import SimpleNamespace


def test_last_exam_answer_returns_completion_before_part2_messages(monkeypatch) -> None:
    from skill_assessment.services import telegram_examination as mod

    qrow = SimpleNamespace(phase="questions", id="exam1")
    prow = SimpleNamespace(phase="protocol", id="exam1")

    monkeypatch.setattr(mod, "_resolve_binding", lambda *_a, **_k: ("client1", "employee1"))
    monkeypatch.setattr(mod.ex, "get_or_create_active_examination_session", lambda *_a, **_k: qrow)
    monkeypatch.setattr(mod.ex, "get_current_question", lambda *_a, **_k: SimpleNamespace(seq=4, text="Вопрос 5"))
    monkeypatch.setattr(mod.ex, "post_answer", lambda *_a, **_k: None)
    monkeypatch.setattr(mod.ex, "get_examination_session", lambda *_a, **_k: prow)
    monkeypatch.setattr(mod.ex, "complete_examination_session", lambda *_a, **_k: None)

    import skill_assessment.services.part2_case as p2

    monkeypatch.setattr(
        p2,
        "on_examination_completed_advance_skill_assessment",
        lambda *_a, **_k: {
            "advanced": True,
            "part2_notice": {
                "sent": True,
                "messages": [
                    "Этап 2: решение кейсов.",
                    "Кейс 1 из 2.\n\nВозникла рабочая ситуация...",
                ],
            },
        },
    )

    out = mod.handle_telegram_message(db=object(), telegram_chat_id="300398364", text="Ответ 5", is_start_command=False)

    assert out[0].startswith("Опрос по регламентам завершён.")
    assert out[1] == "Этап 2: решение кейсов."
    assert "Кейс 1 из 2." in out[2]
