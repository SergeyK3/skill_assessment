from __future__ import annotations

from skill_assessment.adapters.telegram_outbound import HttpxTelegramOutbound


def test_httpx_outbound_omits_reply_markup_when_none(monkeypatch) -> None:
    captured: dict = {}

    class _Resp:
        status_code = 200
        is_success = True
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"ok": True}

    def _fake_post(url, json, timeout):  # noqa: ANN001
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)
    outbound = HttpxTelegramOutbound()
    out = outbound.send_message(
        token="123456789:token",
        chat_id="300398364",
        text="hello",
        reply_markup=None,
    )
    assert out.ok is True
    assert "reply_markup" not in captured["json"]


def test_httpx_outbound_keeps_reply_markup_object(monkeypatch) -> None:
    captured: dict = {}

    class _Resp:
        status_code = 200
        is_success = True
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"ok": True}

    def _fake_post(url, json, timeout):  # noqa: ANN001
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)
    outbound = HttpxTelegramOutbound()
    kb = {"inline_keyboard": [[{"text": "Да", "callback_data": "ok"}]]}
    out = outbound.send_message(
        token="123456789:token",
        chat_id="300398364",
        text="hello",
        reply_markup=kb,
    )
    assert out.ok is True
    assert captured["json"]["reply_markup"] == kb
