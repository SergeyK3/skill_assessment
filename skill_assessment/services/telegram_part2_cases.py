# route: (telegram) | file: skill_assessment/services/telegram_part2_cases.py
"""
Приём ответов по кейсам (этап 2) прямо в Telegram: после рассылки текстов кейсов бот ждёт ответы по одному сообщению на кейс.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.domain.entities import AssessmentSessionStatus, SessionPhase
from skill_assessment.domain.examination_entities import ExaminationSessionStatus as ExamSessionStatus
from skill_assessment.infrastructure.db_models import AssessmentSessionRow, ExaminationSessionRow
from skill_assessment.schemas.api import Part2CaseAnswerIn, Part2CasesSubmit
from skill_assessment.services import examination_service as examination_svc
from skill_assessment.services.part2_case import (
    _chunk_text_for_telegram,
    _dump_cases_payload,
    _parse_cases_payload,
    _telegram_case_message,
    submit_session_cases,
)
from skill_assessment.services.telegram_docs_survey import _telegram_chat_ids_equal

_log = logging.getLogger(__name__)

_TELEGRAM_ANSWER_FLOW_KEY = "telegram_answer_flow"


def _examination_blocks_part2_case_replies(db: Session, client_id: str, employee_id: str) -> bool:
    """
    Пока у сотрудника идёт экзамен по регламентам (Telegram), ответы не забирает сценарий кейсов Part 2.

    Иначе при phase=part2 и оставшемся telegram_answer_flow голос на «Вопрос N» ошибочно
    обрабатывается как ответ на кейс («кейс 1 из 2»).
    """
    cid = (client_id or "").strip()
    eid = (employee_id or "").strip()
    if not cid or not eid:
        return False
    active = db.scalars(
        select(ExaminationSessionRow)
        .where(
            ExaminationSessionRow.client_id == cid,
            ExaminationSessionRow.employee_id == eid,
            ExaminationSessionRow.status.in_(
                (ExamSessionStatus.SCHEDULED.value, ExamSessionStatus.IN_PROGRESS.value)
            ),
        )
        .order_by(ExaminationSessionRow.created_at.desc())
        .limit(1)
    ).first()
    return active is not None


def _resolve_part2_session_for_chat(db: Session, telegram_chat_id: str) -> AssessmentSessionRow | None:
    tid = str(telegram_chat_id).strip()
    bind = examination_svc.get_telegram_binding(db, tid)
    if bind is not None:
        row = db.scalar(
            select(AssessmentSessionRow)
            .where(
                AssessmentSessionRow.client_id == (bind.client_id or "").strip(),
                AssessmentSessionRow.employee_id == (bind.employee_id or "").strip(),
                AssessmentSessionRow.phase == SessionPhase.PART2.value,
                AssessmentSessionRow.status == AssessmentSessionStatus.IN_PROGRESS.value,
            )
            .order_by(AssessmentSessionRow.updated_at.desc())
            .limit(1)
        )
        if row is not None:
            return row

    dev_c = (os.getenv("TELEGRAM_DEV_CLIENT_ID") or "").strip()
    dev_e = (os.getenv("TELEGRAM_DEV_EMPLOYEE_ID") or "").strip()
    if dev_c and dev_e:
        row = db.scalar(
            select(AssessmentSessionRow)
            .where(
                AssessmentSessionRow.client_id == dev_c,
                AssessmentSessionRow.employee_id == dev_e,
                AssessmentSessionRow.phase == SessionPhase.PART2.value,
                AssessmentSessionRow.status == AssessmentSessionStatus.IN_PROGRESS.value,
            )
            .order_by(AssessmentSessionRow.updated_at.desc())
            .limit(1)
        )
        if row is not None:
            return row

    rows = db.scalars(
        select(AssessmentSessionRow)
        .where(
            AssessmentSessionRow.docs_survey_notify_chat_id.isnot(None),
            AssessmentSessionRow.employee_id.isnot(None),
            AssessmentSessionRow.phase == SessionPhase.PART2.value,
            AssessmentSessionRow.status == AssessmentSessionStatus.IN_PROGRESS.value,
        )
        .order_by(AssessmentSessionRow.updated_at.desc())
        .limit(400)
    ).all()
    for r in rows:
        stored = (r.docs_survey_notify_chat_id or "").strip()
        if stored and _telegram_chat_ids_equal(stored, tid):
            return r
    return None


def handle_part2_telegram_message(db: Session, telegram_chat_id: str, text: str, is_start_command: bool) -> list[str]:
    """
    Пустой список — передать дальше (опрос по регламентам и т.д.).
    Иначе ответы пользователю по сценарию «ответы на кейсы в чате».
    """
    if is_start_command:
        return []

    row = _resolve_part2_session_for_chat(db, telegram_chat_id)
    if row is None:
        return []

    if row.client_id and row.employee_id and _examination_blocks_part2_case_replies(
        db, row.client_id, row.employee_id
    ):
        return []

    payload = _parse_cases_payload(getattr(row, "part2_cases_json", None))
    if bool(payload.get("completed")):
        return []

    flow = payload.get(_TELEGRAM_ANSWER_FLOW_KEY)
    if not isinstance(flow, dict) or "awaiting_index" not in flow:
        return []

    cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    if not cases:
        return []

    msg = (text or "").strip()
    if not msg:
        return []

    try:
        awaiting = int(flow.get("awaiting_index", 0))
    except (TypeError, ValueError):
        awaiting = 0

    answers_map: dict[str, str] = flow.get("answers") if isinstance(flow.get("answers"), dict) else {}
    answers_map = {str(k): str(v) for k, v in answers_map.items()}

    if awaiting >= len(cases):
        return []

    case_row = cases[awaiting]
    case_id = str(case_row.get("case_id") or "").strip()
    if not case_id:
        return ["Внутренняя ошибка: у кейса нет идентификатора. Обратитесь в HR."]

    answers_map[case_id] = msg
    awaiting += 1
    flow["answers"] = answers_map
    flow["awaiting_index"] = awaiting
    payload[_TELEGRAM_ANSWER_FLOW_KEY] = flow
    row.part2_cases_json = _dump_cases_payload(payload)
    db.commit()
    db.refresh(row)

    n = len(cases)
    if awaiting < n:
        next_case = cases[awaiting]
        next_block = _telegram_case_message(awaiting + 1, n, str(next_case.get("text") or ""))
        out = [f"Ответ по кейсу {awaiting} из {n} принят."]
        out.extend(_chunk_text_for_telegram(next_block))
        return out

    ordered_answers: list[Part2CaseAnswerIn] = []
    for i, c in enumerate(cases, start=1):
        cid = str(c.get("case_id") or "").strip()
        if not cid:
            continue
        ans = (answers_map.get(cid) or "").strip()
        if not ans:
            return [f"Не хватает ответа по кейсу. Отправьте ответ на кейс {i} из {n}."]
        ordered_answers.append(Part2CaseAnswerIn(case_id=cid, answer=ans))

    try:
        submit_session_cases(db, row.id, Part2CasesSubmit(answers=ordered_answers))
    except HTTPException as e:
        detail = str(e.detail) if e.detail is not None else "submit_failed"
        _log.warning("telegram part2: submit failed session=%s detail=%s", row.id[:8], detail)
        return [f"Не удалось сохранить ответы: {detail}. Попробуйте ещё раз или откройте веб-страницу кейсов."]
    except Exception:
        _log.exception("telegram part2: submit failed session=%s", row.id[:8])
        return ["Ошибка при сохранении ответов. Обратитесь в HR или используйте веб-страницу кейсов."]

    return ["Все ответы по кейсам приняты. Выполняем оценку и обновляем протокол."]
