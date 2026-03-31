"""Единый диспетчер Telegram: маршрутизация с process-context (phase 2 orchestration)."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.domain.examination_entities import ExaminationSessionStatus
from skill_assessment.infrastructure.db_models import ExaminationSessionRow
from skill_assessment.services.telegram_examination import _resolve_binding, handle_telegram_message
from skill_assessment.services.telegram_part2_cases import (
    _resolve_part2_session_for_chat,
    handle_part2_telegram_message,
)
from skill_assessment.services.telegram_process_context import (
    FLOW_EXAM,
    FLOW_IDLE,
    FLOW_PART2,
    get_process_context,
    set_process_context,
)

_log = logging.getLogger(__name__)


def _active_examination_session_id_for_pair(db: Session, client_id: str, employee_id: str) -> str | None:
    cid = (client_id or "").strip()
    eid = (employee_id or "").strip()
    if not cid or not eid:
        return None
    row = db.scalars(
        select(ExaminationSessionRow)
        .where(
            ExaminationSessionRow.client_id == cid,
            ExaminationSessionRow.employee_id == eid,
            ExaminationSessionRow.status.in_(
                (
                    ExaminationSessionStatus.SCHEDULED.value,
                    ExaminationSessionStatus.IN_PROGRESS.value,
                )
            ),
        )
        .order_by(ExaminationSessionRow.created_at.desc())
        .limit(1)
    ).first()
    return str(row.id) if row is not None else None


def dispatch_dialog_message(
    db: Session, telegram_chat_id: str, text: str | None, is_start_command: bool
) -> list[str]:
    """
    Детерминированная маршрутизация:
    1) если у пары client/employee идёт экзамен — сообщение уходит в exam;
    2) иначе пробуем Part2 (кейс-ответы в чате);
    3) иначе fallback в exam (с его стандартной логикой /start и привязок).
    """
    tid = str(telegram_chat_id).strip()
    ctx = get_process_context(db, tid)
    pair = _resolve_binding(db, tid)

    # 1) Экзамен имеет строгий приоритет, если по сотруднику есть активная экзаменационная сессия.
    if pair is not None:
        cid, eid = pair
        exam_sid = _active_examination_session_id_for_pair(db, cid, eid)
        if exam_sid:
            _log.debug("telegram_dispatcher: route=exam chat=%s pair=%s/%s", tid, cid, eid)
            set_process_context(
                db,
                tid,
                flow=FLOW_EXAM,
                client_id=cid,
                employee_id=eid,
                active_session_id=exam_sid,
            )
            return handle_telegram_message(db, tid, text, is_start_command)

    # 2) Если в контексте уже был активен Part2, сначала пытаемся удержать этот поток.
    if ctx is not None and ctx.active_flow == FLOW_PART2:
        part2_lines = handle_part2_telegram_message(db, tid, text or "", is_start_command)
        if part2_lines:
            part2_row = _resolve_part2_session_for_chat(db, tid)
            set_process_context(
                db,
                tid,
                flow=FLOW_PART2,
                client_id=part2_row.client_id if part2_row is not None else ctx.client_id,
                employee_id=part2_row.employee_id if part2_row is not None else ctx.employee_id,
                active_session_id=part2_row.id if part2_row is not None else ctx.active_session_id,
            )
            _log.debug("telegram_dispatcher: route=part2_ctx chat=%s", tid)
            return part2_lines

    # 3) Обычный сценарий: пробуем Part2, затем fallback в экзамен.
    part2_lines = handle_part2_telegram_message(db, tid, text or "", is_start_command)
    if part2_lines:
        part2_row = _resolve_part2_session_for_chat(db, tid)
        set_process_context(
            db,
            tid,
            flow=FLOW_PART2,
            client_id=part2_row.client_id if part2_row is not None else (pair[0] if pair else None),
            employee_id=part2_row.employee_id if part2_row is not None else (pair[1] if pair else None),
            active_session_id=part2_row.id if part2_row is not None else None,
        )
        _log.debug("telegram_dispatcher: route=part2 chat=%s", tid)
        return part2_lines

    lines = handle_telegram_message(db, tid, text, is_start_command)
    pair_after = _resolve_binding(db, tid)
    if pair_after is not None:
        exam_sid_after = _active_examination_session_id_for_pair(db, pair_after[0], pair_after[1])
        if exam_sid_after:
            set_process_context(
                db,
                tid,
                flow=FLOW_EXAM,
                client_id=pair_after[0],
                employee_id=pair_after[1],
                active_session_id=exam_sid_after,
            )
        else:
            set_process_context(db, tid, flow=FLOW_IDLE, client_id=pair_after[0], employee_id=pair_after[1])
    _log.debug("telegram_dispatcher: route=exam_fallback chat=%s", tid)
    return lines
