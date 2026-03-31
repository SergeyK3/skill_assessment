# route: (service examination) | file: skill_assessment/services/examination_service.py

from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from skill_assessment.domain.examination_entities import (
    ConsentStatus,
    ExaminationPhase,
    ExaminationSessionStatus,
)
from skill_assessment.domain.entities import AssessmentSessionStatus, SessionPhase
from skill_assessment.integration.hr_core import (
    employee_display_label,
    get_employee,
    get_examination_regulation_reference_text,
)
from skill_assessment.services.docs_survey_time import utc_naive_to_local_display
from skill_assessment.services.examination_protocol_scores import (
    average_scores,
    score_4_to_percent,
    semantic_or_heuristic_score_4,
)
from skill_assessment.services.stt_service import is_stt_mock_transcript
from skill_assessment.services.examination_question_plan import compose_examination_question_plan
from skill_assessment.infrastructure.db_models import (
    AssessmentSessionRow,
    ExaminationAnswerRow,
    ExaminationQuestionRow,
    ExaminationSessionRow,
    ExaminationTelegramBindingRow,
)
from skill_assessment.schemas.examination_api import (
    ExaminationAnswerBody,
    ExaminationConsentBody,
    ExaminationIntroDoneBody,
    ExaminationProtocolItemOut,
    ExaminationProtocolOut,
    ExaminationQuestionOut,
    ExaminationSessionCreate,
    ExaminationSessionOut,
)
from skill_assessment.services.examination_seed import SCENARIO_REGULATION_V1

_log = logging.getLogger(__name__)


def _answer_timeout_minutes() -> int:
    raw = (os.getenv("SKILL_ASSESSMENT_EXAM_ANSWER_TIMEOUT_MINUTES") or "5").strip()
    try:
        return max(1, int(raw or "5"))
    except ValueError:
        return 5


_EXAM_ANSWER_TIMEOUT_MINUTES = _answer_timeout_minutes()


def _cancel_latest_part1_assessment_session_for_pair(db: Session, client_id: str, employee_id: str) -> bool:
    """Fail-fast: если Part1 сорван, блокируем переходы в Part2/Part3 для этой пары."""
    cid = (client_id or "").strip()
    eid = (employee_id or "").strip()
    if not cid or not eid:
        return False
    sa_row = db.scalar(
        select(AssessmentSessionRow)
        .where(
            AssessmentSessionRow.client_id == cid,
            AssessmentSessionRow.employee_id == eid,
            AssessmentSessionRow.phase == SessionPhase.PART1.value,
            AssessmentSessionRow.status.in_(
                (
                    AssessmentSessionStatus.DRAFT.value,
                    AssessmentSessionStatus.IN_PROGRESS.value,
                )
            ),
        )
        .order_by(AssessmentSessionRow.updated_at.desc())
        .limit(1)
    )
    if sa_row is None:
        return False
    sa_row.status = AssessmentSessionStatus.CANCELLED.value
    sa_row.phase = SessionPhase.PART1.value
    sa_row.completed_at = datetime.now(timezone.utc)
    sa_row.docs_survey_exam_gate_awaiting = False
    db.commit()
    db.refresh(sa_row)
    return True


def _org_unit_display_name(db: Session, client_id: str, org_unit_id: str | None) -> str | None:
    if not org_unit_id:
        return None
    try:
        from app.hr import get_org_unit  # type: ignore[import-not-found]

        ou = get_org_unit(db, client_id, org_unit_id)
        if ou is None:
            return None
        for attr in ("name", "title", "label", "display_name", "code"):
            v = getattr(ou, attr, None)
            if isinstance(v, str) and v.strip():
                return v.strip()
    except ImportError:
        return None
    except Exception:
        _log.debug("examination: get_org_unit for label", exc_info=True)
    return None


def _fio_tuple(emp) -> tuple[str, str, str]:
    """Фамилия, имя, отчество для протокола (EmployeeSnapshot | None)."""
    if emp is None:
        return ("—", "—", "—")
    if emp.last_name or emp.first_name or emp.middle_name:
        return (
            (emp.last_name or "—").strip() or "—",
            (emp.first_name or "—").strip() or "—",
            (emp.middle_name or "—").strip() or "—",
        )
    dn = (emp.display_name or "").strip()
    if not dn:
        return ("—", "—", "—")
    parts = dn.split()
    if len(parts) >= 3:
        return (parts[0], parts[1], " ".join(parts[2:]))
    if len(parts) == 2:
        return (parts[0], parts[1], "—")
    return (parts[0], "—", "—")


def _reference_for_scoring(db: Session, client_id: str, employee_id: str, question_text: str) -> str:
    reg = get_examination_regulation_reference_text(db, client_id, employee_id)
    q = (question_text or "").strip()
    if reg:
        return f"{reg.strip()}\n\n{q}".strip()
    return q


def _collect_answer_scores(
    db: Session, session_id: str, scenario_id: str, client_id: str, employee_id: str
) -> list[int]:
    qs = _ordered_questions(db, scenario_id)
    answers = {
        a.question_id: a
        for a in db.scalars(
            select(ExaminationAnswerRow).where(ExaminationAnswerRow.session_id == session_id)
        ).all()
    }
    scores: list[int] = []
    for q in qs:
        ans = answers.get(q.id)
        txt = ans.transcript_text if ans else ""
        ref = _reference_for_scoring(db, client_id, employee_id, q.text)
        scores.append(semantic_or_heuristic_score_4(txt, ref))
    return scores


def _question_scenario_id(row: ExaminationSessionRow) -> str:
    """Набор вопросов: общий ``regulation_v1`` или свой список (сессия = scenario_id вопросов)."""
    return (row.question_scenario_id or row.scenario_id or SCENARIO_REGULATION_V1).strip()


def apply_no_regulation_block(db: Session, row: ExaminationSessionRow) -> None:
    """Нет регламента/KPI: остановка сценария и одно уведомление кадрам в Telegram."""
    prev = ExaminationPhase(row.phase)
    if prev == ExaminationPhase.BLOCKED_NO_REGULATION:
        return
    row.phase = ExaminationPhase.BLOCKED_NO_REGULATION.value
    row.status = ExaminationSessionStatus.SCHEDULED.value
    row.consent_status = ConsentStatus.PENDING.value
    row.needs_hr_release = False
    row.current_question_index = 0
    db.commit()
    db.refresh(row)
    try:
        from skill_assessment.services.examination_regulation_notify import (
            notify_hr_no_examination_regulation,
        )

        notify_hr_no_examination_regulation(db, row)
    except Exception:
        _log.exception("examination: notify HR (no regulation) failed")


def _last_answer_at(db: Session, session_id: str) -> datetime | None:
    return db.scalars(
        select(ExaminationAnswerRow.created_at)
        .where(ExaminationAnswerRow.session_id == session_id)
        .order_by(ExaminationAnswerRow.created_at.desc())
        .limit(1)
    ).first()


def _utc_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _interrupt_for_answer_timeout(
    db: Session,
    row: ExaminationSessionRow,
    *,
    last_answer_at: datetime | None = None,
    notify_hr: bool = True,
) -> bool:
    ph = ExaminationPhase(row.phase)
    if ph == ExaminationPhase.INTERRUPTED_TIMEOUT:
        return True
    if ph != ExaminationPhase.QUESTIONS:
        return False
    if last_answer_at is None:
        last_answer_at = _last_answer_at(db, row.id)
    last_answer_at = _utc_aware(last_answer_at)
    if last_answer_at is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=_EXAM_ANSWER_TIMEOUT_MINUTES)
    if last_answer_at >= cutoff:
        return False
    row.phase = ExaminationPhase.INTERRUPTED_TIMEOUT.value
    row.status = ExaminationSessionStatus.CANCELLED.value
    row.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    _cancel_latest_part1_assessment_session_for_pair(db, row.client_id, row.employee_id)
    if notify_hr:
        try:
            from skill_assessment.services.examination_regulation_notify import (
                notify_hr_examination_timeout,
            )

            notify_hr_examination_timeout(
                db,
                row,
                last_answer_at=last_answer_at,
                timeout_minutes=_EXAM_ANSWER_TIMEOUT_MINUTES,
            )
        except Exception:
            _log.exception("examination: notify HR (answer timeout) failed")
    return True


def ensure_not_answer_timed_out(db: Session, row: ExaminationSessionRow, *, notify_hr: bool = True) -> ExaminationSessionRow:
    _interrupt_for_answer_timeout(db, row, notify_hr=notify_hr)
    return row


def seed_session_questions_from_hr(db: Session, row: ExaminationSessionRow) -> None:
    if row.question_scenario_id:
        return
    texts = compose_examination_question_plan(db, row.client_id, row.employee_id, seed_key=row.id)
    if texts is None:
        return
    if len(texts) == 0:
        apply_no_regulation_block(db, row)
        return
    qsid = row.id
    for seq, text in enumerate(texts):
        db.add(
            ExaminationQuestionRow(
                id=str(uuid.uuid4()),
                scenario_id=qsid,
                seq=seq,
                text=text,
            )
        )
    row.question_scenario_id = qsid
    db.commit()
    db.refresh(row)


def _ordered_questions(db: Session, scenario_id: str) -> list[ExaminationQuestionRow]:
    q = (
        select(ExaminationQuestionRow)
        .where(ExaminationQuestionRow.scenario_id == scenario_id)
        .order_by(ExaminationQuestionRow.seq.asc())
    )
    return list(db.scalars(q).all())


def _question_count(db: Session, scenario_id: str) -> int:
    return len(_ordered_questions(db, scenario_id))


def _ordered_questions_for_row(db: Session, row: ExaminationSessionRow) -> list[ExaminationQuestionRow]:
    if ExaminationPhase(row.phase) in (ExaminationPhase.BLOCKED_NO_REGULATION, ExaminationPhase.INTERRUPTED_TIMEOUT):
        return []
    return _ordered_questions(db, _question_scenario_id(row))


def _find_related_assessment_session(db: Session, exam_row: ExaminationSessionRow) -> AssessmentSessionRow | None:
    return db.scalar(
        select(AssessmentSessionRow)
        .where(
            AssessmentSessionRow.client_id == exam_row.client_id,
            AssessmentSessionRow.employee_id == exam_row.employee_id,
            AssessmentSessionRow.status != AssessmentSessionStatus.CANCELLED.value,
        )
        .order_by(AssessmentSessionRow.updated_at.desc())
        .limit(1)
    )


def _related_assessment_protocol_context(
    db: Session, exam_row: ExaminationSessionRow
) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
    sa_row = _find_related_assessment_session(db, exam_row)
    if sa_row is None:
        return (None, None, None, None, None, None)
    report_url = None
    report_path = None
    part2_summary = None
    try:
        from skill_assessment.services import part2_case as part2_case_svc
        from skill_assessment.services import part1_docs_checklist as part1_docs_svc

        report_url = part2_case_svc.build_public_report_absolute_url(db, sa_row.id)
        token = getattr(sa_row, "part1_docs_access_token", None) or part1_docs_svc.ensure_part1_docs_access_token(db, sa_row)
        report_path = part2_case_svc.build_public_report_path(token)
        part2_summary = part2_case_svc.get_part2_summary(sa_row)
    except Exception:
        _log.debug("examination: failed to build related assessment protocol context", exc_info=True)
    return (
        sa_row.id,
        getattr(sa_row, "phase", None),
        getattr(sa_row, "status", None),
        report_url,
        report_path,
        part2_summary,
    )


def _session_out(db: Session, row: ExaminationSessionRow, *, include_access_token: bool = False) -> ExaminationSessionOut:
    ph = ExaminationPhase(row.phase)
    if ph in (ExaminationPhase.BLOCKED_NO_REGULATION, ExaminationPhase.INTERRUPTED_TIMEOUT):
        n = 0
    else:
        n = _question_count(db, _question_scenario_id(row))
    return ExaminationSessionOut(
        id=row.id,
        client_id=row.client_id,
        employee_id=row.employee_id,
        scenario_id=row.scenario_id,
        status=ExaminationSessionStatus(row.status),
        phase=ExaminationPhase(row.phase),
        consent_status=ConsentStatus(row.consent_status),
        needs_hr_release=bool(row.needs_hr_release),
        needs_hr_regulation_release=(ph == ExaminationPhase.BLOCKED_NO_REGULATION),
        current_question_index=int(row.current_question_index),
        question_count=n,
        access_window_starts_at=row.access_window_starts_at,
        access_window_ends_at=row.access_window_ends_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        evaluated_at=row.completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        access_token=(row.access_token if include_access_token else None),
    )


def _insert_examination_session_row(
    db: Session,
    *,
    client_id: str,
    employee_id: str,
    scenario_id: str,
    access_window_starts_at,
    access_window_ends_at,
) -> ExaminationSessionRow:
    sid = str(uuid.uuid4())
    row = ExaminationSessionRow(
        id=sid,
        client_id=client_id,
        employee_id=employee_id,
        scenario_id=scenario_id,
        status=ExaminationSessionStatus.SCHEDULED.value,
        phase=ExaminationPhase.CONSENT.value,
        consent_status=ConsentStatus.PENDING.value,
        needs_hr_release=False,
        current_question_index=0,
        access_window_starts_at=access_window_starts_at,
        access_window_ends_at=access_window_ends_at,
        started_at=None,
        completed_at=None,
        access_token=secrets.token_urlsafe(32),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def create_examination_session(db: Session, body: ExaminationSessionCreate) -> ExaminationSessionOut:
    if body.scenario_id != SCENARIO_REGULATION_V1:
        raise HTTPException(status_code=400, detail="unsupported_scenario_mvp")
    if _question_count(db, body.scenario_id) == 0:
        raise HTTPException(status_code=500, detail="examination_questions_not_seeded")
    row = _insert_examination_session_row(
        db,
        client_id=body.client_id,
        employee_id=body.employee_id,
        scenario_id=body.scenario_id,
        access_window_starts_at=body.access_window_starts_at,
        access_window_ends_at=body.access_window_ends_at,
    )
    seed_session_questions_from_hr(db, row)
    return _session_out(db, row, include_access_token=True)


def upsert_telegram_binding(db: Session, client_id: str, employee_id: str, telegram_chat_id: str) -> dict[str, str]:
    tid = str(telegram_chat_id).strip()
    if not tid:
        raise HTTPException(status_code=400, detail="telegram_chat_id_required")
    row = db.scalars(
        select(ExaminationTelegramBindingRow).where(ExaminationTelegramBindingRow.telegram_chat_id == tid)
    ).first()
    if row is None:
        row = ExaminationTelegramBindingRow(
            id=str(uuid.uuid4()),
            telegram_chat_id=tid,
            client_id=client_id,
            employee_id=employee_id,
        )
        db.add(row)
    else:
        row.client_id = client_id
        row.employee_id = employee_id
    db.commit()
    db.refresh(row)
    return {"id": row.id, "telegram_chat_id": row.telegram_chat_id, "client_id": row.client_id, "employee_id": row.employee_id}


def get_telegram_binding(db: Session, telegram_chat_id: str) -> ExaminationTelegramBindingRow | None:
    tid = str(telegram_chat_id).strip()
    return db.scalars(
        select(ExaminationTelegramBindingRow).where(ExaminationTelegramBindingRow.telegram_chat_id == tid)
    ).first()


def get_telegram_binding_for_employee(db: Session, client_id: str, employee_id: str) -> ExaminationTelegramBindingRow | None:
    """Привязка Telegram к сотруднику (если ранее регистрировали POST …/examination/telegram/bindings)."""
    return db.scalars(
        select(ExaminationTelegramBindingRow).where(
            ExaminationTelegramBindingRow.client_id == client_id,
            ExaminationTelegramBindingRow.employee_id == employee_id,
        )
    ).first()


def get_or_create_active_examination_session(db: Session, client_id: str, employee_id: str) -> ExaminationSessionRow:
    """Активная (не завершённая) сессия или новая."""
    if _question_count(db, SCENARIO_REGULATION_V1) == 0:
        raise HTTPException(status_code=500, detail="examination_questions_not_seeded")
    q = (
        select(ExaminationSessionRow)
        .where(
            ExaminationSessionRow.client_id == client_id,
            ExaminationSessionRow.employee_id == employee_id,
            ExaminationSessionRow.status.in_(
                (
                    ExaminationSessionStatus.SCHEDULED.value,
                    ExaminationSessionStatus.IN_PROGRESS.value,
                )
            ),
        )
        .order_by(ExaminationSessionRow.created_at.desc())
    )
    row = db.scalars(q).first()
    if row is not None:
        ensure_not_answer_timed_out(db, row)
        # Если найденная «активная» сессия прямо сейчас перешла в interrupted_timeout
        # (или стала неактивной), не возвращаем её: новая назначенная проверка должна
        # стартовать с новой сессии, а не застревать в старом таймауте.
        try:
            ph = ExaminationPhase(row.phase)
        except ValueError:
            ph = None
        if (
            row.status in (ExaminationSessionStatus.SCHEDULED.value, ExaminationSessionStatus.IN_PROGRESS.value)
            and ph != ExaminationPhase.INTERRUPTED_TIMEOUT
        ):
            return row
    row = _insert_examination_session_row(
        db,
        client_id=client_id,
        employee_id=employee_id,
        scenario_id=SCENARIO_REGULATION_V1,
        access_window_starts_at=None,
        access_window_ends_at=None,
    )
    seed_session_questions_from_hr(db, row)
    return row


def get_examination_session(db: Session, session_id: str) -> ExaminationSessionOut:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    ensure_not_answer_timed_out(db, row)
    return _session_out(db, row, include_access_token=False)


def get_examination_session_by_access_token(db: Session, access_token: str) -> ExaminationSessionOut:
    """Сессия по секрету из персональной ссылки (веб без входа в портал)."""
    t = (access_token or "").strip()
    if not t:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    row = db.scalars(
        select(ExaminationSessionRow).where(ExaminationSessionRow.access_token == t)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    ensure_not_answer_timed_out(db, row)
    return _session_out(db, row, include_access_token=True)


def _enrich_session_row(db: Session, row: ExaminationSessionRow) -> ExaminationSessionOut:
    base = _session_out(db, row)
    emp = get_employee(db, row.client_id, row.employee_id)
    display = employee_display_label(emp)
    dept = _org_unit_display_name(db, row.client_id, emp.org_unit_id if emp else None)
    pos = (emp.position_label or "").strip() if emp else None
    avg4: float | None = None
    avg_pct: float | None = None
    if row.status == ExaminationSessionStatus.COMPLETED.value:
        scores = _collect_answer_scores(db, row.id, _question_scenario_id(row), row.client_id, row.employee_id)
        if scores:
            avg4, avg_pct = average_scores(scores)
    return base.model_copy(
        update={
            "employee_display_name": display,
            "employee_position_label": pos or None,
            "employee_department_label": dept,
            "average_score_4": avg4,
            "average_score_percent": avg_pct,
        }
    )


def list_examination_sessions(
    db: Session,
    client_id: str | None,
    employee_id: str | None,
    limit: int = 50,
    status: str | None = None,
    *,
    enrich: bool = False,
) -> list[ExaminationSessionOut]:
    q = select(ExaminationSessionRow).order_by(ExaminationSessionRow.created_at.desc()).limit(limit)
    if client_id:
        q = q.where(ExaminationSessionRow.client_id == client_id)
    if employee_id:
        q = q.where(ExaminationSessionRow.employee_id == employee_id)
    if status:
        q = q.where(ExaminationSessionRow.status == status)
    rows = db.scalars(q).all()
    for r in rows:
        ensure_not_answer_timed_out(db, r)
    if enrich:
        return [_enrich_session_row(db, r) for r in rows]
    return [_session_out(db, r) for r in rows]


def delete_examination_session(db: Session, session_id: str) -> None:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    db.delete(row)
    db.flush()
    db.execute(delete(ExaminationQuestionRow).where(ExaminationQuestionRow.scenario_id == session_id))
    db.commit()


def list_scenario_questions(db: Session, scenario_id: str) -> list[ExaminationQuestionOut]:
    rows = _ordered_questions(db, scenario_id)
    return [
        ExaminationQuestionOut(id=r.id, scenario_id=r.scenario_id, seq=r.seq, text=r.text) for r in rows
    ]


def get_current_question(db: Session, session_id: str) -> ExaminationQuestionOut | None:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    ensure_not_answer_timed_out(db, row)
    if ExaminationPhase(row.phase) == ExaminationPhase.INTERRUPTED_TIMEOUT:
        raise HTTPException(status_code=403, detail="examination_interrupted_timeout")
    if ExaminationPhase(row.phase) != ExaminationPhase.QUESTIONS:
        return None
    qs = _ordered_questions_for_row(db, row)
    idx = int(row.current_question_index)
    if idx < 0 or idx >= len(qs):
        return None
    r = qs[idx]
    return ExaminationQuestionOut(id=r.id, scenario_id=r.scenario_id, seq=r.seq, text=r.text)


def post_consent(db: Session, session_id: str, body: ExaminationConsentBody) -> ExaminationSessionOut:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    if ExaminationPhase(row.phase) == ExaminationPhase.BLOCKED_NO_REGULATION:
        raise HTTPException(status_code=403, detail="examination_blocked_no_regulation")
    if ExaminationPhase(row.phase) == ExaminationPhase.BLOCKED_CONSENT:
        raise HTTPException(status_code=403, detail="consent_blocked_needs_hr")
    if ExaminationPhase(row.phase) == ExaminationPhase.INTERRUPTED_TIMEOUT:
        raise HTTPException(status_code=403, detail="examination_interrupted_timeout")
    if ExaminationPhase(row.phase) != ExaminationPhase.CONSENT:
        raise HTTPException(status_code=400, detail="consent_not_expected_in_this_phase")
    if not body.accepted:
        row.consent_status = ConsentStatus.DECLINED.value
        row.phase = ExaminationPhase.BLOCKED_CONSENT.value
        row.needs_hr_release = True
        db.commit()
        db.refresh(row)
        _cancel_latest_part1_assessment_session_for_pair(db, row.client_id, row.employee_id)
        return _session_out(db, row)
    row.consent_status = ConsentStatus.ACCEPTED.value
    row.phase = ExaminationPhase.INTRO.value
    row.status = ExaminationSessionStatus.IN_PROGRESS.value
    if row.started_at is None:
        row.started_at = datetime.now(timezone.utc)
    row.needs_hr_release = False
    db.commit()
    db.refresh(row)
    return _session_out(db, row)


def hr_release_consent_block(db: Session, session_id: str) -> ExaminationSessionOut:
    """Снимает блок после отказа от согласия (роль HR — заглушка без auth в MVP)."""
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    if ExaminationPhase(row.phase) == ExaminationPhase.BLOCKED_NO_REGULATION:
        raise HTTPException(status_code=400, detail="hr_release_wrong_block_use_regulation_endpoint")
    if ExaminationPhase(row.phase) == ExaminationPhase.INTERRUPTED_TIMEOUT:
        raise HTTPException(status_code=400, detail="hr_release_wrong_block_use_new_session")
    if ExaminationPhase(row.phase) != ExaminationPhase.BLOCKED_CONSENT:
        raise HTTPException(status_code=400, detail="hr_release_not_needed")
    row.phase = ExaminationPhase.CONSENT.value
    row.consent_status = ConsentStatus.PENDING.value
    row.needs_hr_release = False
    db.commit()
    db.refresh(row)
    return _session_out(db, row)


def hr_release_regulation_block(db: Session, session_id: str) -> ExaminationSessionOut:
    """После загрузки регламента/KPI в ядре HR — снять блок и снова подобрать вопросы."""
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    if ExaminationPhase(row.phase) != ExaminationPhase.BLOCKED_NO_REGULATION:
        raise HTTPException(status_code=400, detail="hr_regulation_release_not_needed")
    row.phase = ExaminationPhase.CONSENT.value
    row.consent_status = ConsentStatus.PENDING.value
    row.question_scenario_id = None
    db.execute(delete(ExaminationQuestionRow).where(ExaminationQuestionRow.scenario_id == session_id))
    db.commit()
    db.refresh(row)
    seed_session_questions_from_hr(db, row)
    db.refresh(row)
    return _session_out(db, row)


def post_intro_done(db: Session, session_id: str, body: ExaminationIntroDoneBody) -> ExaminationSessionOut:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    if ExaminationPhase(row.phase) == ExaminationPhase.BLOCKED_NO_REGULATION:
        raise HTTPException(status_code=403, detail="examination_blocked_no_regulation")
    if ExaminationPhase(row.phase) == ExaminationPhase.INTERRUPTED_TIMEOUT:
        raise HTTPException(status_code=403, detail="examination_interrupted_timeout")
    if ExaminationPhase(row.phase) != ExaminationPhase.INTRO:
        raise HTTPException(status_code=400, detail="intro_not_expected")
    if not body.ready:
        raise HTTPException(status_code=400, detail="intro_not_ready")
    row.phase = ExaminationPhase.QUESTIONS.value
    row.current_question_index = 0
    db.commit()
    db.refresh(row)
    return _session_out(db, row)


def post_answer(db: Session, session_id: str, body: ExaminationAnswerBody) -> ExaminationSessionOut:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    ensure_not_answer_timed_out(db, row)
    if ExaminationPhase(row.phase) == ExaminationPhase.BLOCKED_NO_REGULATION:
        raise HTTPException(status_code=403, detail="examination_blocked_no_regulation")
    if ExaminationPhase(row.phase) == ExaminationPhase.INTERRUPTED_TIMEOUT:
        raise HTTPException(status_code=403, detail="examination_interrupted_timeout")
    if ExaminationPhase(row.phase) != ExaminationPhase.QUESTIONS:
        raise HTTPException(status_code=400, detail="answers_not_expected_in_this_phase")
    qs = _ordered_questions_for_row(db, row)
    idx = int(row.current_question_index)
    if idx < 0 or idx >= len(qs):
        raise HTTPException(status_code=400, detail="no_current_question")
    qrow = qs[idx]
    existing = db.scalars(
        select(ExaminationAnswerRow).where(
            ExaminationAnswerRow.session_id == row.id,
            ExaminationAnswerRow.question_id == qrow.id,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="answer_already_recorded_use_resume_flow")
    db.add(
        ExaminationAnswerRow(
            id=str(uuid.uuid4()),
            session_id=row.id,
            question_id=qrow.id,
            transcript_text=body.transcript_text,
        )
    )
    idx_next = idx + 1
    row.current_question_index = idx_next
    if idx_next >= len(qs):
        row.phase = ExaminationPhase.PROTOCOL.value
    db.commit()
    db.refresh(row)
    return _session_out(db, row)


def build_protocol(db: Session, session_id: str) -> ExaminationProtocolOut:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    ensure_not_answer_timed_out(db, row)
    ph = ExaminationPhase(row.phase)
    if ph == ExaminationPhase.BLOCKED_NO_REGULATION:
        raise HTTPException(status_code=403, detail="examination_blocked_no_regulation")
    if ph == ExaminationPhase.INTERRUPTED_TIMEOUT:
        raise HTTPException(status_code=403, detail="examination_interrupted_timeout")
    if ph not in (ExaminationPhase.PROTOCOL, ExaminationPhase.COMPLETED):
        raise HTTPException(status_code=400, detail="protocol_not_ready")
    qs = _ordered_questions_for_row(db, row)
    answers = {
        a.question_id: a
        for a in db.scalars(
            select(ExaminationAnswerRow).where(ExaminationAnswerRow.session_id == row.id)
        ).all()
    }
    items: list[ExaminationProtocolItemOut] = []
    score_list: list[int] = []
    for q in qs:
        ans = answers.get(q.id)
        txt = ans.transcript_text if ans else ""
        ref = _reference_for_scoring(db, row.client_id, row.employee_id, q.text)
        s4 = semantic_or_heuristic_score_4(txt, ref)
        score_list.append(s4)
        items.append(
            ExaminationProtocolItemOut(
                question_id=q.id,
                seq=q.seq,
                question_text=q.text,
                transcript_text=txt,
                score_4=s4,
                score_percent=score_4_to_percent(s4),
            )
        )
    avg4: float | None = None
    avg_pct: float | None = None
    if score_list:
        avg4, avg_pct = average_scores(score_list)

    emp = get_employee(db, row.client_id, row.employee_id)
    ln, fn, mn = _fio_tuple(emp)
    dept = _org_unit_display_name(db, row.client_id, emp.org_unit_id if emp else None)
    pos = (emp.position_label or "").strip() if emp else None

    now = datetime.now(timezone.utc)
    if row.completed_at is not None:
        evaluated_at = row.completed_at
        evaluation_is_preliminary = False
    else:
        evaluated_at = now
        evaluation_is_preliminary = True

    (
        related_assessment_session_id,
        related_assessment_phase,
        related_assessment_status,
        related_report_url,
        related_report_path,
        part2_summary,
    ) = _related_assessment_protocol_context(db, row)

    return ExaminationProtocolOut(
        session_id=row.id,
        scenario_id=row.scenario_id,
        employee_id=row.employee_id,
        client_id=row.client_id,
        items=items,
        completed_at=row.completed_at,
        employee_last_name=ln,
        employee_first_name=fn,
        employee_middle_name=mn,
        employee_position_label=pos or None,
        employee_department_label=dept,
        average_score_4=avg4,
        average_score_percent=avg_pct,
        evaluated_at=evaluated_at,
        evaluation_is_preliminary=evaluation_is_preliminary,
        scoring_note="",
        related_assessment_session_id=related_assessment_session_id,
        related_assessment_phase=related_assessment_phase,
        related_assessment_status=related_assessment_status,
        related_report_url=related_report_url,
        related_report_path=related_report_path,
        part2_summary=part2_summary,
    )


def _html_protocol_answer_body(transcript_text: str | None) -> str:
    """Текст ответа в HTML: реальный транскрипт или пояснение, если в БД только mock STT."""
    from html import escape as he

    t = (transcript_text or "").strip()
    if not t:
        return he("—")
    if is_stt_mock_transcript(t):
        return he(
            "Текст ответа недоступен: распознавание речи не выполнено (режим mock). "
            "Для сохранения транскрипта в протоколе задайте SKILL_ASSESSMENT_STT_PROVIDER=openai "
            "и ключ SKILL_ASSESSMENT_OPENAI_API_KEY (или OPENAI_API_KEY)."
        )
    return he(t)


def render_examination_protocol_html(proto: ExaminationProtocolOut) -> str:
    """HTML-протокол для просмотра в браузере и скачивания (последовательная вёрстка под длинные ответы)."""
    from html import escape as he

    def _fmt_dt(ca) -> str:
        if ca is None:
            return "—"
        s = utc_naive_to_local_display(ca)
        return s if s else "—"

    exam_dt = _fmt_dt(proto.completed_at)
    eval_dt = _fmt_dt(proto.evaluated_at)
    eval_note = ""
    if proto.evaluation_is_preliminary:
        eval_note = (
            " <span style=\"font-size:0.85rem\">(предварительная оценка до завершения экзамена; "
            "дата фиксации совпадёт с датой завершения)</span>"
        )

    parts: list[str] = [
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\"/>",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>",
        "<title>Протокол экзамена по регламентам</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;max-width:42rem;margin:2rem auto;padding:0 1rem;line-height:1.5;}",
        "h1{font-size:1.25rem;margin-bottom:0.35rem;}",
        ".meta{color:#555;font-size:0.9rem;margin:0 0 1rem 0;}",
        ".summary{border:1px solid #d1d5db;background:#f9fafb;border-radius:10px;padding:1rem 1.1rem;margin:1rem 0 1.35rem;}",
        ".summary h2{font-size:1rem;margin:0 0 0.5rem;}",
        ".summary .big{font-size:1.35rem;font-weight:600;letter-spacing:-0.02em;}",
        ".summary .sub{font-size:0.88rem;color:#444;margin-top:0.35rem;}",
        ".note{font-size:0.82rem;color:#555;margin-top:0.65rem;line-height:1.4;}",
        ".qa{border:1px solid #e5e7eb;border-radius:10px;padding:0.85rem 1rem;margin:0 0 1rem;}",
        ".qa h3{font-size:0.82rem;text-transform:uppercase;letter-spacing:0.04em;color:#6b7280;margin:0 0 0.4rem;}",
        ".qa .qtxt{font-size:0.95rem;margin:0 0 0.5rem;}",
        ".qa .marks{font-size:0.88rem;color:#374151;margin:0 0 0.6rem;}",
        ".qa .ans{white-space:pre-wrap;word-wrap:break-word;font-size:0.95rem;padding:0.65rem 0.75rem;background:#fafafa;border-radius:8px;border:1px solid #eee;}",
        "</style></head><body>",
        "<h1>Протокол экзамена по внутренним регламентам</h1>",
        "<p class=\"meta\">",
        f"Сессия: <code>{he(proto.session_id)}</code><br/>",
        f"Фамилия: <strong>{he(proto.employee_last_name)}</strong> · Имя: <strong>{he(proto.employee_first_name)}</strong> · "
        f"Отчество: <strong>{he(proto.employee_middle_name)}</strong><br/>",
        f"Должность: {he(proto.employee_position_label or '—')}<br/>",
        f"Подразделение: {he(proto.employee_department_label or '—')}<br/>",
        f"Идентификатор сотрудника в системе: <code>{he(proto.employee_id)}</code><br/>",
        f"Организация (client_id): <code>{he(proto.client_id)}</code><br/>",
        f"<strong>Дата прохождения экзаменации (завершение сессии):</strong> {he(exam_dt)}<br/>",
        f"<strong>Дата оценки:</strong> {he(eval_dt)}{eval_note}",
        "</p>",
    ]
    if proto.average_score_4 is not None and proto.average_score_percent is not None:
        note_html = ""
        if (proto.scoring_note or "").strip():
            note_html = f"<p class=\"note\">{he(proto.scoring_note)}</p>"
        parts.append(
            "<section class=\"summary\" aria-labelledby=\"hdr-summary\">"
            "<h2 id=\"hdr-summary\">Итоговая (интегральная) оценка по экзамену</h2>"
            f"<div class=\"big\">{proto.average_score_4:.2f} из 4 баллов · {proto.average_score_percent:.1f}%</div>"
            "<div class=\"sub\">Шкала баллов: 1–4. Проценты: 50–100% (минимум 50% соответствует баллу 1).</div>"
            f"{note_html}"
            "</section>"
        )
    if proto.related_assessment_session_id:
        report_href = proto.related_report_url or proto.related_report_path
        report_link_html = (
            f'<p class="sub" style="margin-top:0.55rem;">Общий протокол оценки: '
            f'<a href="{he(report_href)}" target="_blank" rel="noopener">{he(report_href)}</a></p>'
            if report_href
            else ""
        )
        part2_line = (
            f"<p class=\"sub\" style=\"margin-top:0.55rem;\"><strong>Этап 2 (кейсы):</strong> {he(proto.part2_summary)}</p>"
            if (proto.part2_summary or "").strip()
            else ""
        )
        parts.append(
            "<section class=\"summary\" aria-labelledby=\"hdr-related-assessment\">"
            "<h2 id=\"hdr-related-assessment\">Связанный общий протокол оценки</h2>"
            f"<div class=\"sub\">Сквозная сессия: <code>{he(proto.related_assessment_session_id)}</code> · "
            f"phase: <strong>{he(proto.related_assessment_phase or '—')}</strong> · "
            f"status: <strong>{he(proto.related_assessment_status or '—')}</strong></div>"
            f"{part2_line}"
            "<p class=\"note\">После завершения Part 2 и Part 3 этот общий протокол дополняется оценкой по кейсам и оценкой руководителя.</p>"
            f"{report_link_html}"
            "</section>"
        )
    for it in proto.items:
        parts.append(
            "<section class=\"qa\">"
            f"<h3>Вопрос {it.seq + 1}</h3>"
            f"<p class=\"qtxt\">{he(it.question_text)}</p>"
            f"<p class=\"marks\"><strong>Оценка по ответу:</strong> {it.score_4} балла (шкала 1–4) · "
            f"{it.score_percent:.1f}% (шкала 50–100%)</p>"
            f"<p class=\"marks\" style=\"margin-top:0.35rem;font-weight:600;\">Ответ сотрудника (транскрипт / текст)</p>"
            f"<div class=\"ans\">{_html_protocol_answer_body(it.transcript_text)}</div>"
            "</section>"
        )
    parts.append("</body></html>")
    return "".join(parts)


def complete_examination_session(
    db: Session, session_id: str, *, advance_assessment_to_part2: bool = True
) -> ExaminationSessionOut:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    ensure_not_answer_timed_out(db, row)
    if ExaminationPhase(row.phase) == ExaminationPhase.BLOCKED_NO_REGULATION:
        raise HTTPException(status_code=403, detail="examination_blocked_no_regulation")
    if ExaminationPhase(row.phase) == ExaminationPhase.INTERRUPTED_TIMEOUT:
        raise HTTPException(status_code=403, detail="examination_interrupted_timeout")
    if ExaminationPhase(row.phase) != ExaminationPhase.PROTOCOL:
        raise HTTPException(status_code=400, detail="complete_only_from_protocol_phase")
    row.phase = ExaminationPhase.COMPLETED.value
    row.status = ExaminationSessionStatus.COMPLETED.value
    row.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    out = _session_out(db, row)
    if advance_assessment_to_part2:
        try:
            from skill_assessment.services.part2_case import on_examination_completed_advance_skill_assessment

            on_examination_completed_advance_skill_assessment(db, row)
        except Exception:
            _log.exception("examination: advance skill assessment to part2 after complete failed for %s", session_id[:8])
    try:
        from skill_assessment.services.examination_protocol_delivery import (
            schedule_examination_protocol_delivery,
        )

        schedule_examination_protocol_delivery(session_id)
    except Exception:
        _log.exception("examination: schedule protocol Telegram delivery failed after complete")
    return out
