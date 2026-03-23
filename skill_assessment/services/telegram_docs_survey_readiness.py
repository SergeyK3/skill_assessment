# route: (telegram) | file: skill_assessment/services/telegram_docs_survey_readiness.py
"""
Напоминание перед опросом по документам: «готов / не готов» (inline-кнопки).

Callback: ``dsr|y|session_id`` / ``dsr|n|session_id``.

- **Нет** → выбор из 3 ближайших рабочих дней и времени (перенос слота).
- **Да** → моделируется «время пришло»; далее текстовый сценарий в :mod:`telegram_docs_survey_exam_gate`.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from skill_assessment.infrastructure.db_models import AssessmentSessionRow
from skill_assessment.services.telegram_docs_survey import (
    DocsSurveyCallbackResult,
    _chat_owns_session,
    build_docs_survey_slot_keyboard_days,
)

_log = logging.getLogger(__name__)

PREFIX_READINESS = "dsr"

MSG_TIME_ARRIVED_GATE = (
    "Время назначенного опроса по документам. Считаем, что время пришло.\n\n"
    "Готовы ответить на вопросы по внутренним регламентам?\n\n"
    "Напишите «да» (готов) или «нет» (пока не готов)."
)


def build_readiness_inline_keyboard(session_id: str) -> dict[str, Any]:
    cb_y = f"{PREFIX_READINESS}|y|{session_id}"
    cb_n = f"{PREFIX_READINESS}|n|{session_id}"
    for cb in (cb_y, cb_n):
        if len(cb.encode("utf-8")) > 64:
            raise ValueError("callback_data exceeds Telegram 64-byte limit")
    return {
        "inline_keyboard": [
            [{"text": "Да", "callback_data": cb_y}],
            [{"text": "Нет", "callback_data": cb_n}],
        ]
    }


def handle_docs_survey_readiness_callback(
    db: Session, chat_id: str, callback_data: str
) -> DocsSurveyCallbackResult | None:
    data = (callback_data or "").strip()
    if not data.startswith(f"{PREFIX_READINESS}|"):
        return None
    parts = data.split("|")
    if len(parts) != 3:
        return DocsSurveyCallbackResult("Некорректные данные", [])
    _prefix, yn, session_id = parts
    if yn not in ("y", "n") or len(session_id) < 32:
        return DocsSurveyCallbackResult("Некорректные данные", [])

    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        return DocsSurveyCallbackResult("Сессия не найдена", [])

    if not _chat_owns_session(db, session_id, chat_id):
        _log.warning(
            "docs_survey_readiness: нет доступа session=%s chat=%s",
            session_id[:8],
            chat_id,
        )
        return DocsSurveyCallbackResult("Нет доступа к этой сессии", [])

    if yn == "n":
        row.docs_survey_readiness_answer = "not_ready"
        row.docs_survey_exam_gate_awaiting = False
        db.commit()
        db.refresh(row)
        try:
            kb = build_docs_survey_slot_keyboard_days(session_id, 3)
        except ValueError as e:
            _log.warning("readiness keyboard: %s", e)
            return DocsSurveyCallbackResult(
                "Ошибка клавиатуры",
                [(f"Выберите другой день позже. ({e})", None)],
            )
        _log.info("docs_survey_readiness: перенос слота (3 дня) session=%s", session_id[:8])
        return DocsSurveyCallbackResult(
            "Выберите дату",
            [
                (
                    "Вы не отметили готовность к опросу. Выберите другой день и время "
                    "(ближайшие 3 рабочих дня):",
                    kb,
                )
            ],
        )

    row.docs_survey_readiness_answer = "ready"
    row.docs_survey_exam_gate_awaiting = True
    db.commit()
    db.refresh(row)
    _log.info("docs_survey_readiness: готов + ворота экзамена session=%s", session_id[:8])
    return DocsSurveyCallbackResult("Принято", [(MSG_TIME_ARRIVED_GATE, None)])
