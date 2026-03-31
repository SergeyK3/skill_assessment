from __future__ import annotations

from skill_assessment.services import telegram_dispatcher as td


def test_dispatch_prefers_exam_when_active(monkeypatch):
    monkeypatch.setattr(td, "get_process_context", lambda db, chat_id: None)
    monkeypatch.setattr(td, "_resolve_binding", lambda db, chat_id: ("c1", "e1"))
    monkeypatch.setattr(td, "_active_examination_session_id_for_pair", lambda db, client_id, employee_id: "ex1")
    monkeypatch.setattr(td, "set_process_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(td, "handle_part2_telegram_message", lambda db, chat_id, text, is_start: ["part2"])
    monkeypatch.setattr(td, "handle_telegram_message", lambda db, chat_id, text, is_start: ["exam"])

    out = td.dispatch_dialog_message(db=object(), telegram_chat_id="42", text="hello", is_start_command=False)
    assert out == ["exam"]


def test_dispatch_prefers_part2_without_active_exam(monkeypatch):
    monkeypatch.setattr(td, "get_process_context", lambda db, chat_id: None)
    monkeypatch.setattr(td, "_resolve_binding", lambda db, chat_id: ("c1", "e1"))
    monkeypatch.setattr(td, "_active_examination_session_id_for_pair", lambda db, client_id, employee_id: None)
    monkeypatch.setattr(td, "set_process_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(td, "_resolve_part2_session_for_chat", lambda db, chat_id: None)
    monkeypatch.setattr(td, "handle_part2_telegram_message", lambda db, chat_id, text, is_start: ["part2"])
    monkeypatch.setattr(td, "handle_telegram_message", lambda db, chat_id, text, is_start: ["exam"])

    out = td.dispatch_dialog_message(db=object(), telegram_chat_id="42", text="hello", is_start_command=False)
    assert out == ["part2"]


def test_dispatch_falls_back_to_exam_when_part2_empty(monkeypatch):
    monkeypatch.setattr(td, "get_process_context", lambda db, chat_id: None)
    monkeypatch.setattr(td, "_resolve_binding", lambda db, chat_id: None)
    monkeypatch.setattr(td, "set_process_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(td, "handle_part2_telegram_message", lambda db, chat_id, text, is_start: [])
    monkeypatch.setattr(td, "handle_telegram_message", lambda db, chat_id, text, is_start: ["exam"])

    out = td.dispatch_dialog_message(db=object(), telegram_chat_id="42", text="hello", is_start_command=False)
    assert out == ["exam"]
