from __future__ import annotations

from types import SimpleNamespace


def test_notify_hr_suppressed_when_hr_chat_equals_employee_chat(monkeypatch) -> None:
    from skill_assessment.services import docs_survey_hr_notify as mod

    monkeypatch.setenv("TELEGRAM_DOCS_SURVEY_HR_NOTIFY_CHAT_ID", "300398364")
    monkeypatch.setenv("TELEGRAM_DOCS_SURVEY_SUPPRESS_HR_NOTIFY_TO_EMPLOYEE_CHAT", "1")
    monkeypatch.setattr(mod, "_load_env", lambda: None)
    monkeypatch.setattr(mod, "get_employee", lambda db, client_id, employee_id: None)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "send_telegram_text_to_chat", lambda chat_id, text: sent.append((chat_id, text)) or True)

    row = SimpleNamespace(
        id="12345678-1234-1234-1234-1234567890ab",
        client_id="c1",
        employee_id="e1",
        docs_survey_notify_chat_id="300398364",
    )
    ok = mod.notify_hr_docs_survey_consent_issue(db=None, row=row, reason="timeout")
    assert ok is False
    assert sent == []
