# route: (telegram) | file: skill_assessment/services/telegram_docs_survey_exam_gate.py
"""
Если до экзамена ещё ждали текстовое «да» (старый поток): ответ обрабатывается здесь.

После inline «Да» в напоминании экзамен запускается сразу — см.
:func:`start_examination_immediately_for_assessment_session`.

Если пользователь отвечает «нет» на ворота — предлагаем перенос слота (3 рабочих дня), как после «не готов» в напоминании.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.domain.examination_entities import ExaminationPhase
from skill_assessment.infrastructure.db_models import AssessmentSessionRow
from skill_assessment.schemas.examination_api import ExaminationConsentBody, ExaminationIntroDoneBody
from skill_assessment.services import examination_service as ex
from skill_assessment.services.telegram_docs_survey import (
    _telegram_chat_ids_equal,
    build_docs_survey_slot_keyboard_days,
)
from skill_assessment.services.telegram_examination import _is_yes

_log = logging.getLogger(__name__)

GATE_REPEAT = (
    "Время назначенного опроса по документам. Готовы ответить на вопросы по внутренним регламентам?\n\n"
    "Напишите «да» (готов) или «нет» (пока не готов)."
)


def _find_gate_session(db: Session, telegram_chat_id: str) -> AssessmentSessionRow | None:
    tid = str(telegram_chat_id).strip()
    rows = db.scalars(
        select(AssessmentSessionRow).where(AssessmentSessionRow.docs_survey_exam_gate_awaiting.is_(True))
    ).all()
    matching = [
        r
        for r in rows
        if r.docs_survey_notify_chat_id and _telegram_chat_ids_equal(str(r.docs_survey_notify_chat_id), tid)
    ]
    if not matching:
        return None
    matching.sort(key=lambda x: x.updated_at or x.created_at or datetime(1970, 1, 1), reverse=True)
    return matching[0]


def start_examination_immediately_for_assessment_session(
    db: Session, assessment_row: AssessmentSessionRow
) -> list[tuple[str, dict[str, Any] | None]]:
    """
    После «Да» в напоминании о готовности — без ожидания времени слота и без второго текстового подтверждения.
    """
    if not assessment_row.employee_id:
        return [("Не удалось сопоставить сотрудника с экзаменом. Обратитесь в HR.", None)]

    assessment_row.docs_survey_exam_gate_awaiting = False
    db.commit()
    db.refresh(assessment_row)

    try:
        ex_row = ex.get_or_create_active_examination_session(db, assessment_row.client_id, assessment_row.employee_id)
    except HTTPException as e:
        return [(f"Не удалось открыть сессию экзамена: {e.detail}", None)]

    phase = ExaminationPhase(ex_row.phase)

    if phase == ExaminationPhase.BLOCKED_NO_REGULATION:
        return [
            (
                "Экзамен приостановлен: для вашей должности и подразделения не найден регламент с KPI в системе. "
                "Отдел кадров получил уведомление. После загрузки регламента напишите сюда снова.",
                None,
            )
        ]
    if phase == ExaminationPhase.BLOCKED_CONSENT:
        return [
            (
                "Согласие на экзамен заблокировано. Обратитесь в HR, затем снова напишите в этот чат.",
                None,
            )
        ]
    if phase == ExaminationPhase.INTERRUPTED_TIMEOUT:
        return [
            (
                "Экзамен прерван по таймауту: между ответами прошло более 5 минут. "
                "Отдел кадров получил уведомление. Для повторной проверки потребуется новое назначение.",
                None,
            )
        ]

    if phase == ExaminationPhase.CONSENT:
        out = ex.post_consent(db, ex_row.id, ExaminationConsentBody(accepted=True))
        if ExaminationPhase(out.phase) == ExaminationPhase.BLOCKED_CONSENT:
            return [("Отказ зафиксирован. Обратитесь в отдел кадров для повторного предложения согласия.", None)]
        ex.post_intro_done(db, ex_row.id, ExaminationIntroDoneBody())
    elif phase == ExaminationPhase.INTRO:
        ex.post_intro_done(db, ex_row.id, ExaminationIntroDoneBody())
    elif phase == ExaminationPhase.QUESTIONS:
        pass
    elif phase == ExaminationPhase.PROTOCOL:
        return [("Сейчас отображается протокол экзамена. Завершите его по подсказкам бота.", None)]
    elif phase == ExaminationPhase.COMPLETED:
        return [("Экзамен по регламентам уже завершён. При новом назначении откроется новая сессия.", None)]
    else:
        return [("Неизвестная фаза экзамена. Напишите в этот чат.", None)]

    q = ex.get_current_question(db, ex_row.id)
    if q:
        return [
            (
                f"Вопрос {q.seq + 1}:\n\n{q.text}\n\n"
                "Ответ — одним сообщением: текстом или голосом (будет записан в протокол как транскрипт).",
                None,
            )
        ]
    return [("Нет вопросов в сценарии (ошибка конфигурации).", None)]


def handle_exam_gate_message(
    db: Session, telegram_chat_id: str, text: str | None, is_start_command: bool
) -> list[tuple[str, dict[str, Any] | None]]:
    """
    Пустой список — передать дальше (сценарий опроса Part1 или экзамен).
    Иначе список (текст, reply_markup) для отправки.
    """
    row = _find_gate_session(db, telegram_chat_id)
    if row is None:
        return []

    msg = (text or "").strip()
    if not msg:
        return [(GATE_REPEAT, None)]

    if is_start_command:
        # Не перехватываем /start: дальше handle_telegram_message (короткий текст согласия на этап, если ПДн по опросу уже принят).
        return []

    yn = _is_yes(msg)
    if yn is None:
        return [("Не понял. Напишите «да» (готов) или «нет» (пока не готов).", None)]

    if yn is False:
        row.docs_survey_exam_gate_awaiting = False
        row.docs_survey_readiness_answer = "not_ready"
        row.docs_survey_reminder_30m_sent_at = None
        db.commit()
        db.refresh(row)
        try:
            kb = build_docs_survey_slot_keyboard_days(row.id, 3)
        except Exception:
            kb = None
        return [
            (
                "Понял. Выберите новый день и время (ближайшие 3 рабочих дня):",
                kb,
            )
        ]

    if not row.employee_id:
        return [
            (
                "Не удалось сопоставить сотрудника с экзаменом. Обратитесь в HR.",
                None,
            )
        ]

    try:
        ex_row = ex.get_or_create_active_examination_session(db, row.client_id, row.employee_id)
    except Exception as e:
        _log.exception("exam_gate: get_or_create examination")
        return [(f"Ошибка сессии экзамена: {e}", None)]

    phase = ExaminationPhase(ex_row.phase)

    if phase == ExaminationPhase.CONSENT:
        # Реальное согласие и переход INTRO — в handle_telegram_message; иначе «да» только зацикливало текст-инструкцию.
        from skill_assessment.services.telegram_examination import handle_telegram_message

        submsgs = handle_telegram_message(db, telegram_chat_id, text, False)
        row.docs_survey_exam_gate_awaiting = False
        db.commit()
        return [(m, None) for m in submsgs if m]

    if phase == ExaminationPhase.BLOCKED_CONSENT:
        return [
            (
                "Согласие на экзамен заблокировано. Обратитесь в HR, затем снова напишите в этот чат.",
                None,
            )
        ]
    if phase == ExaminationPhase.INTERRUPTED_TIMEOUT:
        return [
            (
                "Экзамен прерван по таймауту: между ответами прошло более 5 минут. "
                "Отдел кадров получил уведомление. Для повторной проверки потребуется новое назначение.",
                None,
            )
        ]

    if phase == ExaminationPhase.INTRO:
        try:
            ex.post_intro_done(db, ex_row.id, ExaminationIntroDoneBody())
        except Exception as e:
            _log.exception("exam_gate: post_intro_done")
            return [(f"Не удалось начать вопросы: {e}", None)]

        row.docs_survey_exam_gate_awaiting = False
        db.commit()
        q = ex.get_current_question(db, ex_row.id)
        if q:
            return [
                (
                    f"Вопрос {q.seq + 1}:\n\n{q.text}\n\nОтправьте ответ одним сообщением.",
                    None,
                )
            ]
        return [("Нет вопросов в сценарии (ошибка конфигурации).", None)]

    if phase == ExaminationPhase.QUESTIONS:
        row.docs_survey_exam_gate_awaiting = False
        db.commit()
        return []

    if phase == ExaminationPhase.PROTOCOL:
        row.docs_survey_exam_gate_awaiting = False
        db.commit()
        return []

    if phase == ExaminationPhase.COMPLETED:
        row.docs_survey_exam_gate_awaiting = False
        db.commit()
        return []

    row.docs_survey_exam_gate_awaiting = False
    db.commit()
    return []
