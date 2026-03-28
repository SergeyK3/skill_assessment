# route: (telegram) | file: skill_assessment/services/telegram_docs_survey_consent.py
"""
Согласие на обработку ПДн для опроса по служебным документам (Part 1) в Telegram.

Первое сообщение — текст + inline «Да»/«Нет» (callback ``dsp|…``). После «Да» — выбор даты/времени.
Текстовые ответы «да»/«нет» по-прежнему обрабатываются как запасной вариант.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.domain.entities import AssessmentSessionStatus
from skill_assessment.domain.examination_entities import ExaminationPhase, ExaminationSessionStatus as ExamSessionStatus
from skill_assessment.infrastructure.db_models import AssessmentSessionRow, ExaminationSessionRow
from skill_assessment.services.docs_survey_hr_notify import notify_hr_docs_survey_consent_issue
from skill_assessment.services.part1_docs_checklist import telegram_part1_docs_checklist_message_line
from skill_assessment.services.telegram_docs_survey import (
    DocsSurveyCallbackResult,
    _chat_owns_session,
    build_docs_survey_slot_keyboard,
)

_log = logging.getLogger(__name__)

PREFIX_PD = "dsp"

MSG_SCHEDULING_AFTER_CONSENT = (
    "Предлагаем согласовать с HR (отдел кадров) удобную дату и время опроса в течение следующих "
    "5 рабочих дней. Опрос займет от 15 до 30 минут."
)

DOCS_SURVEY_PD_CONSENT_PROMPT = (
    "Нажмите кнопки «Да» или «Нет» под предыдущим сообщением бота. "
    "Либо ответьте одним сообщением: «да» или «нет»."
)

MSG_DECLINED = (
    "Отказ от согласия зафиксирован. Участие в опросе без согласия невозможно — при необходимости обратитесь в HR."
)

MSG_UNCLEAR = "Не понял ответ. Напишите «да» (согласие) или «нет» (отказ), либо используйте кнопки под сообщением."


def _parse_pd_consent_yes_no(text: str) -> bool | None:
    """Да/нет для согласия (без «готов», чтобы не путать с другими сценариями)."""
    t = text.strip().lower()
    if not t:
        return None
    if t in ("да", "yes", "ok", "ага", "+", "согласен", "согласна", "принимаю", "принимаю согласие"):
        return True
    if t in ("нет", "no", "отказ", "отказываюсь", "-", "не согласен", "не согласна"):
        return False
    if re.match(r"^да[\s!.]*$", t) or (t.startswith("да ") and len(t) < 80):
        return True
    if re.match(r"^нет[\s!.]*$", t) or (t.startswith("нет ") and len(t) < 80):
        return False
    return None


def _telegram_chat_ids_equal(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    x, y = str(a).strip(), str(b).strip()
    if x == y:
        return True
    if x.isdigit() and y.isdigit():
        return int(x) == int(y)
    return False


def build_pd_consent_inline_keyboard(session_id: str) -> dict[str, Any]:
    """Две кнопки: Да / Нет. ``callback_data`` ≤ 64 байт (UUID в идентификаторе сессии)."""
    cb_y = f"{PREFIX_PD}|y|{session_id}"
    cb_n = f"{PREFIX_PD}|n|{session_id}"
    for cb in (cb_y, cb_n):
        if len(cb.encode("utf-8")) > 64:
            raise ValueError("callback_data exceeds Telegram 64-byte limit")
    return {
        "inline_keyboard": [
            [{"text": "Да", "callback_data": cb_y}],
            [{"text": "Нет", "callback_data": cb_n}],
        ]
    }


def _row_superseded_by_accepted_pd_elsewhere(db: Session, row: AssessmentSessionRow) -> bool:
    """
    Другая активная сессия того же сотрудника уже приняла ПДн по опросу — строка ``awaiting_first`` устарела
    (дубликат чата / незакрытый статус), её нельзя использовать для перехвата сообщений.
    """
    cid = (row.client_id or "").strip()
    eid = (row.employee_id or "").strip()
    if not cid or not eid:
        return False
    other = db.scalar(
        select(AssessmentSessionRow.id)
        .where(
            AssessmentSessionRow.client_id == cid,
            AssessmentSessionRow.employee_id == eid,
            AssessmentSessionRow.docs_survey_pd_consent_status == "accepted",
            AssessmentSessionRow.id != row.id,
            AssessmentSessionRow.status.notin_(
                (AssessmentSessionStatus.COMPLETED.value, AssessmentSessionStatus.CANCELLED.value)
            ),
        )
        .limit(1)
    )
    return other is not None


def _get_active_examination_session_row(
    db: Session, client_id: str, employee_id: str
) -> ExaminationSessionRow | None:
    """Существующая незавершённая сессия экзамена по регламентам (без создания новой)."""
    cid, eid = (client_id or "").strip(), (employee_id or "").strip()
    if not cid or not eid:
        return None
    return db.scalars(
        select(ExaminationSessionRow)
        .where(
            ExaminationSessionRow.client_id == cid,
            ExaminationSessionRow.employee_id == eid,
            ExaminationSessionRow.status != ExamSessionStatus.COMPLETED.value,
        )
        .order_by(ExaminationSessionRow.created_at.desc())
        .limit(1)
    ).first()


def _examination_blocks_pd_consent(db: Session, telegram_chat_id: str) -> bool:
    """
    Пользователь уже в сценарии экзамена (вопросы, вступление и т.д.) — не перехватывать ответы
    обработчиком согласия ПДн для Part1 (иначе длинный транскрипт голоса даёт MSG_UNCLEAR).
    """
    from skill_assessment.services.telegram_examination import _resolve_binding

    pair = _resolve_binding(db, str(telegram_chat_id).strip())
    if pair is None:
        return False
    exam_row = _get_active_examination_session_row(db, pair[0], pair[1])
    if exam_row is None:
        return False
    try:
        ph = ExaminationPhase(exam_row.phase)
    except ValueError:
        return False
    return ph in (
        ExaminationPhase.CONSENT,
        ExaminationPhase.INTRO,
        ExaminationPhase.QUESTIONS,
        ExaminationPhase.PROTOCOL,
        ExaminationPhase.BLOCKED_CONSENT,
        ExaminationPhase.BLOCKED_NO_REGULATION,
    )


def _find_awaiting_first_consent_session(db: Session, chat_id: str) -> AssessmentSessionRow | None:
    tid = str(chat_id).strip()
    rows = db.scalars(
        select(AssessmentSessionRow).where(AssessmentSessionRow.docs_survey_pd_consent_status == "awaiting_first")
    ).all()
    matching = [
        r
        for r in rows
        if r.docs_survey_notify_chat_id and _telegram_chat_ids_equal(str(r.docs_survey_notify_chat_id), tid)
    ]
    if not matching:
        return None
    matching.sort(key=lambda x: x.updated_at or x.created_at or datetime(1970, 1, 1), reverse=True)
    for candidate in matching:
        if _row_superseded_by_accepted_pd_elsewhere(db, candidate):
            continue
        return candidate
    return None


def handle_pd_consent_callback(db: Session, chat_id: str, callback_data: str) -> DocsSurveyCallbackResult | None:
    """Inline «Да»/«Нет» по согласию ПДн. Префикс ``dsp|``."""
    data = (callback_data or "").strip()
    if not data.startswith(f"{PREFIX_PD}|"):
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
            "pd_consent: нет доступа session=%s chat=%s notify_chat=%r employee_id=%r",
            session_id[:8],
            chat_id,
            (row.docs_survey_notify_chat_id or "")[:40],
            row.employee_id,
        )
        return DocsSurveyCallbackResult("Нет доступа к этой сессии", [])

    st = row.docs_survey_pd_consent_status or ""
    if st == "accepted":
        return DocsSurveyCallbackResult("Согласие уже получено.", [])
    if st in ("declined", "timed_out"):
        return DocsSurveyCallbackResult("Ответ по согласию уже зафиксирован.", [])
    if st != "awaiting_first":
        return DocsSurveyCallbackResult("Согласие сейчас не ожидается.", [])

    now = datetime.now(timezone.utc)
    row.docs_survey_pd_consent_at = now

    if yn == "y":
        row.docs_survey_pd_consent_status = "accepted"
        db.commit()
        db.refresh(row)
        kb = build_docs_survey_slot_keyboard(row.id)
        outgoing: list[tuple[str, dict[str, Any] | None]] = []
        line = telegram_part1_docs_checklist_message_line(db, session_id)
        if line:
            outgoing.append((line, None))
        outgoing.append((MSG_SCHEDULING_AFTER_CONSENT, kb))
        return DocsSurveyCallbackResult("Выберите дату", outgoing)

    row.docs_survey_pd_consent_status = "declined"
    db.commit()
    db.refresh(row)
    sent = notify_hr_docs_survey_consent_issue(db, row, "declined")
    if sent:
        row.docs_survey_hr_notified_no_consent_at = now
        db.commit()
        db.refresh(row)

    return DocsSurveyCallbackResult(
        "Отказ зафиксирован",
        [(MSG_DECLINED, None)],
    )


def handle_docs_survey_pd_consent_message(
    db: Session, telegram_chat_id: str, text: str | None, is_start_command: bool
) -> list[tuple[str, dict[str, Any] | None]]:
    """
    Обрабатывает ответ по согласию ПДн до показа календаря.
    Пустой список — передать управление другим сценариям (экзамен).
    Каждый элемент: (текст, reply_markup или None).
    """
    if _examination_blocks_pd_consent(db, telegram_chat_id):
        return []
    row = _find_awaiting_first_consent_session(db, telegram_chat_id)
    if row is None:
        return []

    msg = (text or "").strip()
    if not msg or is_start_command:
        return [(DOCS_SURVEY_PD_CONSENT_PROMPT, None)]

    yn = _parse_pd_consent_yes_no(msg)
    if yn is None:
        return [(MSG_UNCLEAR, None)]

    now = datetime.now(timezone.utc)
    row.docs_survey_pd_consent_at = now
    if yn:
        row.docs_survey_pd_consent_status = "accepted"
        db.commit()
        db.refresh(row)
        kb = build_docs_survey_slot_keyboard(row.id)
        out_msg: list[tuple[str, dict[str, Any] | None]] = []
        line = telegram_part1_docs_checklist_message_line(db, row.id)
        if line:
            out_msg.append((line, None))
        out_msg.append((MSG_SCHEDULING_AFTER_CONSENT, kb))
        return out_msg
    row.docs_survey_pd_consent_status = "declined"
    db.commit()
    db.refresh(row)
    sent = notify_hr_docs_survey_consent_issue(db, row, "declined")
    if sent:
        row.docs_survey_hr_notified_no_consent_at = now
        db.commit()
        db.refresh(row)
    return [(MSG_DECLINED, None)]
