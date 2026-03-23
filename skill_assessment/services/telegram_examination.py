# route: (telegram + examination) | file: skill_assessment/services/telegram_examination.py
"""
Обработка сообщений Telegram в сценарии экзамена (после привязки chat_id к сотруднику).
"""

from __future__ import annotations

import os
import re

from fastapi import HTTPException
from sqlalchemy.orm import Session

from skill_assessment.domain.examination_entities import ExaminationPhase
from skill_assessment.schemas.examination_api import (
    ExaminationAnswerBody,
    ExaminationConsentBody,
    ExaminationIntroDoneBody,
)
from skill_assessment.services import examination_service as ex

CONSENT_PROMPT = (
    "Согласие на обработку персональных данных и результатов проверки (экзамен по регламентам).\n"
    "Продолжая, вы подтверждаете ознакомление с политикой организации.\n\n"
    "Ответьте одним сообщением:\n"
    "• «да» или «согласен» — принять\n"
    "• «нет» или «отказ» — отказаться (потребуется помощь HR)"
)

INTRO_PROMPT = (
    "Экзамен по внутренним регламентам и должностным инструкциям.\n"
    "Ориентировочное время — до 30 минут, вопросы по очереди.\n\n"
    "Когда будете готовы, напишите: готов"
)


def _resolve_binding(db: Session, chat_id: str) -> tuple[str, str] | None:
    row = ex.get_telegram_binding(db, chat_id)
    if row is not None:
        return row.client_id, row.employee_id
    dev_c = os.getenv("TELEGRAM_DEV_CLIENT_ID", "").strip()
    dev_e = os.getenv("TELEGRAM_DEV_EMPLOYEE_ID", "").strip()
    if dev_c and dev_e:
        return dev_c, dev_e
    return None


def _is_yes(text: str) -> bool | None:
    """True = да, False = нет, None = непонятно."""
    t = text.strip().lower()
    if not t:
        return None
    if t in ("да", "yes", "ok", "ага", "+", "согласен", "согласна", "принимаю", "готов", "готова"):
        return True
    if t in ("нет", "no", "отказ", "отказываюсь", "-", "не согласен", "не согласна"):
        return False
    if re.match(r"^да[\s!.]*$", t) or t.startswith("да "):
        return True
    if re.match(r"^нет[\s!.]*$", t) or t.startswith("нет "):
        return False
    return None


def _is_ready(text: str) -> bool:
    t = text.strip().lower()
    return t in ("готов", "готова", "да", "yes", "ok", "начать", "поехали", "давай")


def _is_done(text: str) -> bool:
    """Завершение просмотра протокола (не путать с «готов» на этапе intro)."""
    t = text.strip().lower()
    return t in ("готово", "завершить", "ок", "ok", "да", "спасибо")


def handle_telegram_message(db: Session, telegram_chat_id: str, text: str | None, is_start_command: bool) -> list[str]:
    """
    Возвращает список сообщений для отправки пользователю (по одному или несколько абзацев).
    """
    tid = str(telegram_chat_id).strip()
    pair = _resolve_binding(db, tid)
    if pair is None:
        return [
            "Этот чат ещё не привязан к сотруднику.\n\n"
            "Попросите HR выполнить привязку (API POST "
            "/api/skill-assessment/examination/telegram/bindings) "
            "или задайте в .env для разработки TELEGRAM_DEV_CLIENT_ID и TELEGRAM_DEV_EMPLOYEE_ID."
        ]

    client_id, employee_id = pair
    try:
        row = ex.get_or_create_active_examination_session(db, client_id, employee_id)
    except HTTPException as e:
        return [f"Не удалось открыть сессию экзамена: {e.detail}"]

    phase = ExaminationPhase(row.phase)
    msg = (text or "").strip()

    try:
        if phase == ExaminationPhase.CONSENT:
            if not msg or is_start_command:
                return [CONSENT_PROMPT]
            yn = _is_yes(msg)
            if yn is None:
                return ["Не понял ответ. Напишите «да» (согласен) или «нет» (отказ)."]
            out = ex.post_consent(db, row.id, ExaminationConsentBody(accepted=yn))
            if ExaminationPhase(out.phase) == ExaminationPhase.BLOCKED_CONSENT:
                return ["Отказ зафиксирован. Обратитесь в отдел кадров для повторного предложения согласия."]
            return [INTRO_PROMPT]

        if phase == ExaminationPhase.BLOCKED_CONSENT:
            return [
                "Согласие заблокировано до действий HR. "
                "После снятия блока снова напишите /start."
            ]

        if phase == ExaminationPhase.INTRO:
            if not msg or (is_start_command and not _is_ready(msg)):
                return [INTRO_PROMPT]
            if not _is_ready(msg):
                return ["Когда будете готовы начать вопросы, напишите «готов»."]
            ex.post_intro_done(db, row.id, ExaminationIntroDoneBody())
            row = ex.get_examination_session(db, row.id)
            q = ex.get_current_question(db, row.id)
            if q:
                return [f"Вопрос {q.seq + 1}:\n\n{q.text}\n\nОтправьте ответ одним сообщением."]
            return ["Нет вопросов в сценарии (ошибка конфигурации)."]

        if phase == ExaminationPhase.QUESTIONS:
            qcur = ex.get_current_question(db, row.id)
            if not qcur:
                return ["Внутренняя ошибка: нет текущего вопроса."]
            if not msg:
                return [f"Вопрос {qcur.seq + 1}:\n\n{qcur.text}\n\nОтправьте ответ текстом."]
            ex.post_answer(db, row.id, ExaminationAnswerBody(transcript_text=msg))
            row = ex.get_examination_session(db, row.id)
            if ExaminationPhase(row.phase) == ExaminationPhase.PROTOCOL:
                proto = ex.build_protocol(db, row.id)
                lines = ["Протокол сформирован. Кратко:"]
                for it in proto.items[:8]:
                    lines.append(f"\n— В{it.seq + 1}: {it.transcript_text[:200]}{'…' if len(it.transcript_text) > 200 else ''}")
                lines.append("\n\nНапишите «готово», чтобы завершить экзамен.")
                return ["".join(lines)]

            qnext = ex.get_current_question(db, row.id)
            if qnext:
                return [
                    "Ответ записан.\n\n"
                    f"Вопрос {qnext.seq + 1}:\n\n{qnext.text}\n\nОтправьте ответ одним сообщением."
                ]
            return ["Ответ записан."]

        if phase == ExaminationPhase.PROTOCOL:
            if _is_done(msg):
                ex.complete_examination_session(db, row.id)
                return [
                    "Экзамен завершён. Спасибо за уделённое время. "
                    "Протокол доступен в системе для отдела кадров."
                ]
            if not msg:
                return ["Напишите «готово», когда закончите просмотр протокола."]
            return ["Напишите «готово», чтобы завершить экзамен (или коротко «да» / «ок»)."]

        if phase == ExaminationPhase.COMPLETED:
            return ["Этот экзамен уже завершён. При новом назначении HR откроется новая сессия."]

    except HTTPException as e:
        return [f"Ошибка: {e.detail}"]

    return ["Неизвестное состояние сценария. Напишите /start или обратитесь в поддержку."]
