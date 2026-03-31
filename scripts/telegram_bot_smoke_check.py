#!/usr/bin/env python3
# route: (diagnostic) | file: scripts/telegram_bot_smoke_check.py
"""
Проверка бота без перезапуска uvicorn: токен, режим mock, тестовая отправка, конфликт getUpdates.

Запуск из каталога skill_assessment (рядом с пакетом)::

    python scripts/telegram_bot_smoke_check.py

Или из любого места, если в PYTHONPATH есть корень репозитория::

    python path/to/skill_assessment/scripts/telegram_bot_smoke_check.py

Переменные (подхватываются из skill_assessment/.env)::

    TELEGRAM_BOT_TOKEN
    TELEGRAM_DOCS_SURVEY_FALLBACK_CHAT_ID  — куда слать тест (если не задано — только getMe)
    SKILL_ASSESSMENT_TELEGRAM_OUTBOUND=mock — предупреждение, реальная отправка не выполняется

Опции окружения для скрипта::

    TELEGRAM_SMOKE_SKIP_SEND=1   — не вызывать sendMessage (только getMe + getUpdates)
    TELEGRAM_SMOKE_CHAT_ID=…    — явный chat_id для тестового сообщения
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _ensure_plugin_on_path() -> None:
    here = Path(__file__).resolve()
    plugin_root = here.parent.parent
    if str(plugin_root) not in sys.path:
        sys.path.insert(0, str(plugin_root))


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    _ensure_plugin_on_path()
    from skill_assessment.env import load_plugin_env

    load_plugin_env(override=False)

    try:
        import httpx
    except ImportError:
        print("FAIL: нужен пакет httpx (pip install httpx)")
        return 2

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    mock = (os.getenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND") or "").strip().lower() == "mock"
    skip_send = (os.getenv("TELEGRAM_SMOKE_SKIP_SEND") or "").strip().lower() in ("1", "true", "yes", "on")
    explicit_chat = (os.getenv("TELEGRAM_SMOKE_CHAT_ID") or "").strip()
    fallback_chat = (os.getenv("TELEGRAM_DOCS_SURVEY_FALLBACK_CHAT_ID") or "").strip()
    chat_for_send = explicit_chat or fallback_chat

    lines: list[str] = []
    ok_token = False
    ok_send = None  # None = не делали
    conflict_updates = False

    lines.append("=== Проверка Telegram-бота (skill_assessment) ===\n")

    if mock:
        lines.append(
            "WARN: SKILL_ASSESSMENT_TELEGRAM_OUTBOUND=mock — приложение не шлёт в Telegram, "
            "исходящие идут в заглушку. Для реальных сообщений уберите mock из .env."
        )

    if not token or len(token) < 10:
        lines.append("FAIL: TELEGRAM_BOT_TOKEN не задан или слишком короткий.")
        _print_verdict(lines, ok_token=False, ok_send=None, conflict=False)
        return 1

    base = f"https://api.telegram.org/bot{token}"

    with httpx.Client(timeout=30.0) as client:
        # 1) Токен
        try:
            r = client.get(f"{base}/getMe")
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        except Exception as e:
            lines.append(f"FAIL: getMe — сеть/ошибка: {e}")
            _print_verdict(lines, ok_token=False, ok_send=None, conflict=False)
            return 1

        if r.is_success and isinstance(data, dict) and data.get("ok") and data.get("result"):
            u = data["result"]
            uname = u.get("username", "?")
            lines.append(f"OK: токен действителен (бот @{uname}).")
            ok_token = True
        else:
            desc = data.get("description") if isinstance(data, dict) else r.text[:300]
            lines.append(f"FAIL: getMe HTTP {r.status_code} — {desc}")
            _print_verdict(lines, ok_token=False, ok_send=None, conflict=False)
            return 1

        # 2) Конфликт long polling (другой процесс уже держит getUpdates)
        try:
            r2 = client.get(f"{base}/getUpdates", params={"offset": -1, "timeout": 0, "limit": 1})
            d2 = r2.json() if r2.headers.get("content-type", "").startswith("application/json") else {}
            if isinstance(d2, dict) and d2.get("error_code") == 409:
                conflict_updates = True
                lines.append(
                    "WARN: getUpdates вернул 409 Conflict — другой экземпляр уже опрашивает этого бота. "
                    "Оставьте один процесс (один telegram_worker или встроенный polling). Иначе входящие сбоят."
                )
            elif r2.is_success and isinstance(d2, dict) and d2.get("ok"):
                lines.append("OK: getUpdates доступен (конфликта long polling не видно).")
            else:
                desc = d2.get("description") if isinstance(d2, dict) else r2.text[:200]
                lines.append(f"INFO: getUpdates — HTTP {r2.status_code} {desc}")
        except Exception as e:
            lines.append(f"INFO: getUpdates не проверили: {e}")

        # 3) Тестовая отправка
        if skip_send:
            lines.append("SKIP: TELEGRAM_SMOKE_SKIP_SEND=1 — sendMessage не вызывался.")
            ok_send = None
        elif mock:
            lines.append("SKIP: режим mock — sendMessage не выполняется (как в приложении).")
            ok_send = None
        elif not chat_for_send:
            lines.append(
                "SKIP: нет chat_id для теста (задайте TELEGRAM_SMOKE_CHAT_ID или TELEGRAM_DOCS_SURVEY_FALLBACK_CHAT_ID)."
            )
            ok_send = None
        else:
            text = (
                "Тест skill_assessment: проверка sendMessage. "
                "Если видите это — исходящие от бота до Telegram доходят."
            )
            try:
                r3 = client.post(f"{base}/sendMessage", json={"chat_id": chat_for_send, "text": text})
                d3 = r3.json() if r3.headers.get("content-type", "").startswith("application/json") else {}
                if r3.is_success and isinstance(d3, dict) and d3.get("ok"):
                    lines.append(f"OK: sendMessage доставлен в chat_id={chat_for_send}.")
                    ok_send = True
                else:
                    desc = d3.get("description") if isinstance(d3, dict) else r3.text[:400]
                    lines.append(f"FAIL: sendMessage — HTTP {r3.status_code} — {desc}")
                    ok_send = False
            except Exception as e:
                lines.append(f"FAIL: sendMessage — {e}")
                ok_send = False

    _print_verdict(lines, ok_token=ok_token, ok_send=ok_send, conflict=conflict_updates)
    if ok_send is False:
        return 1
    if not ok_token:
        return 1
    return 0


def _print_verdict(
    lines: list[str],
    *,
    ok_token: bool,
    ok_send: bool | None,
    conflict: bool,
) -> None:
    for line in lines:
        print(line)
    print("\n--- Итог ---")
    print(f"Токен бота (getMe):     {'ДА' if ok_token else 'НЕТ'}")
    if ok_send is None:
        print("Тестовое сообщение:     (не выполнялся)")
    else:
        print(f"Тестовое сообщение:     {'ДА — ушло в чат' if ok_send else 'НЕТ — ошибка выше'}")
    print(f"Конфликт getUpdates:    {'ДА (есть предупреждение)' if conflict else 'не обнаружен'}")
    if conflict:
        print("\nИтоговая оценка «бот отвечает»: входящие команды могут не обрабатываться при 409; "
              "исходящие sendMessage обычно всё равно работают.")
    elif ok_token and ok_send is not False:
        print("\nИтоговая оценка: исходящая связь с Telegram API в порядке (для приёма сообщений нужен один poller).")
    print("---")


if __name__ == "__main__":
    raise SystemExit(main())
