# route: (service) | file: skill_assessment/services/stt_service.py
"""Speech-to-text: OpenAI Whisper API или mock (автотесты / без ключа)."""

from __future__ import annotations

import os

import httpx

# Префикс текста в режиме mock — по нему протокол может подсказать настроить OpenAI Whisper.
STT_MOCK_TRANSCRIPT_PREFIX = "[STT mock]"


class SttConfigurationError(RuntimeError):
    """Нет ключа или провайдер не настроен."""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def max_audio_bytes() -> int:
    """Максимальный размер тела аудио для STT (по умолчанию 25 МБ, лимит Whisper)."""
    return max(1024, _env_int("SKILL_ASSESSMENT_STT_MAX_BYTES", 25 * 1024 * 1024))


def stt_provider() -> str:
    """``mock`` | ``openai``. Если не задано: при наличии ключа OpenAI — ``openai``, иначе ``mock``."""
    raw = os.getenv("SKILL_ASSESSMENT_STT_PROVIDER", "").strip().lower()
    if raw in ("mock", "openai"):
        return raw
    key = _openai_api_key()
    return "openai" if key else "mock"


def _openai_api_key() -> str:
    return (os.getenv("SKILL_ASSESSMENT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()


def transcribe_audio_bytes(
    data: bytes,
    *,
    filename: str = "audio.webm",
    content_type: str | None = None,
) -> str:
    """
    Возвращает текст транскрипции.

    При ``mock`` — детерминированная строка без внешних вызовов.
    При ``openai`` — Whisper ``v1/audio/transcriptions``.
    """
    if not data:
        raise ValueError("empty_audio")
    if len(data) > max_audio_bytes():
        raise ValueError("audio_too_large")

    prov = stt_provider()
    if prov == "mock":
        return f"{STT_MOCK_TRANSCRIPT_PREFIX} Получено {len(data)} байт ({filename})."

    if prov != "openai":
        raise SttConfigurationError(f"unknown_stt_provider:{prov}")

    api_key = _openai_api_key()
    if not api_key:
        raise SttConfigurationError("openai_api_key_missing")

    ct = content_type or "application/octet-stream"
    url = "https://api.openai.com/v1/audio/transcriptions"
    # Whisper принимает webm/ogg/mp3/wav и др.
    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (filename, data, ct)},
            data={
                "model": "whisper-1",
                "language": "ru",
            },
        )
    if r.status_code >= 400:
        detail = r.text[:2000]
        raise RuntimeError(f"openai_stt_http_{r.status_code}:{detail}")

    try:
        payload = r.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("openai_stt_invalid_json") from exc
    text = (payload.get("text") or "").strip()
    if not text:
        raise RuntimeError("openai_stt_empty_text")
    return text


def is_stt_mock_transcript(text: str | None) -> bool:
    """True, если в БД попала строка-заглушка mock STT, а не реальный транскрипт."""
    return (text or "").strip().startswith(STT_MOCK_TRANSCRIPT_PREFIX)
