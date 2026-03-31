from __future__ import annotations

from types import SimpleNamespace


def test_first_part2_answer_sends_second_case_text_without_service_lines(monkeypatch) -> None:
    from skill_assessment.services import telegram_part2_cases as mod
    from skill_assessment.services.part2_case import _dump_cases_payload, _parse_cases_payload

    payload = {
        "cases": [
            {
                "case_id": "c1",
                "text": "Возникла рабочая ситуация 1.",
            },
            {
                "case_id": "c2",
                "text": (
                    "Заголовок: Кейс 2/2 по навыкам ...\n"
                    "Навыки в фокусе: ...\n"
                    "Возникла рабочая ситуация 2. Нужно принять решение."
                ),
            },
        ],
        "telegram_answer_flow": {"awaiting_index": 0, "answers": {}},
        "completed": False,
    }
    row = SimpleNamespace(
        id="sa1",
        client_id="c",
        employee_id="e",
        part2_cases_json=_dump_cases_payload(payload),
    )

    class _FakeDb:
        def commit(self) -> None:
            return None

        def refresh(self, _row) -> None:
            return None

    monkeypatch.setattr(mod, "_resolve_part2_session_for_chat", lambda *_a, **_k: row)
    monkeypatch.setattr(mod, "_examination_blocks_part2_case_replies", lambda *_a, **_k: False)

    out = mod.handle_part2_telegram_message(_FakeDb(), "300398364", "Ответ по кейсу 1", False)
    joined = "\n".join(out)

    assert "Ответ по кейсу 1 из 2 принят." in joined
    assert "Кейс 2 из 2." in joined
    assert "Возникла рабочая ситуация 2" in joined
    assert "Навыки в фокусе" not in joined
    assert "Заголовок:" not in joined

    updated = _parse_cases_payload(row.part2_cases_json)
    flow = updated.get("telegram_answer_flow") or {}
    assert int(flow.get("awaiting_index") or 0) == 1


def test_clean_case_text_for_telegram_removes_skill_exposure() -> None:
    from skill_assessment.services.part2_case import _clean_case_text_for_telegram

    raw = (
        "Ситуация:\n"
        "Вы выполняете роль «Менеджер по продажам». Возникла рабочая ситуация, где нужно не только "
        "показать основной навык «Выполнение плана продаж и прогнозирование», но и одновременно проявить "
        "связанные навыки: Ведение переговоров и закрытие сделок; Работа с возражениями. "
        "Срок на решение ограничен одним рабочим днём. Каковы ваши действия?"
    )

    cleaned = _clean_case_text_for_telegram(raw)

    assert "основной навык" not in cleaned.lower()
    assert "связанные навыки" not in cleaned.lower()
    assert "выполнение плана продаж" not in cleaned.lower()
    assert "возникла рабочая ситуация" in cleaned.lower()
    assert "каковы ваши действия?" in cleaned.lower()
