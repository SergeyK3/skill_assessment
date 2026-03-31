# route: (service layer) | file: skill_assessment/services/assessment_service.py
"""Создание сессий и фиксация результатов."""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from skill_assessment.domain.entities import AssessmentSessionStatus, EvidenceKind, ProficiencyLevel, SessionPhase
from skill_assessment.infrastructure.db_models import (
    AssessmentSessionRow,
    SkillAssessmentResultRow,
    SkillDomainRow,
    SkillRow,
)
from skill_assessment.services.docs_survey_time import (
    aware_utc_to_local_label,
    docs_survey_hr_labels,
    local_slot_to_utc_naive,
    utc_naive_slot_to_local_date_time_strings,
    utc_naive_to_aware_utc,
)
from skill_assessment.services.hr_session_flags import is_hr_no_show, latest_exam_status_label
from skill_assessment.services.part1_docs_checklist import ensure_part1_docs_access_token, is_docs_checklist_completed
from skill_assessment.services.session_competency_matrix import session_competency_skill_map
from skill_assessment.schemas.api import (
    AssessmentSessionOut,
    CaseTextOut,
    DocsSurveySlotManualUpdate,
    ManagerRatingsBulk,
    SessionCancelBody,
    SessionCreate,
    SessionPhaseUpdate,
    SkillAssessmentResultOut,
    SkillDomainOut,
    SkillOut,
    SkillResultCreate,
)


def _parse_part2_payload(row: AssessmentSessionRow) -> dict:
    raw = getattr(row, "part2_cases_json", None)
    if not raw or not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def manager_assessment_deadline_aware_utc(row: AssessmentSessionRow) -> datetime | None:
    """Дедлайн для руководителя: по умолчанию через 72 часа после завершения Part 2."""
    payload = _parse_part2_payload(row)
    base_raw = payload.get("completed_at")
    base_dt: datetime | None = None
    if isinstance(base_raw, str) and base_raw.strip():
        try:
            base_dt = datetime.fromisoformat(base_raw.replace("Z", "+00:00"))
        except ValueError:
            base_dt = None
    if base_dt is None:
        ph = getattr(row, "phase", None) or ""
        if ph not in (SessionPhase.PART3.value, SessionPhase.COMPLETED.value, SessionPhase.REPORT.value):
            return None
        base_dt = utc_naive_to_aware_utc(getattr(row, "updated_at", None)) or datetime.now(timezone.utc)
    if base_dt.tzinfo is None:
        base_dt = base_dt.replace(tzinfo=timezone.utc)
    raw_hours = (os.getenv("SKILL_ASSESSMENT_MANAGER_REVIEW_DEADLINE_HOURS") or "").strip()
    try:
        hours = max(1, min(24 * 30, int(raw_hours)))
    except ValueError:
        hours = 72
    return base_dt.astimezone(timezone.utc) + timedelta(hours=hours)


def manager_assessment_deadline_label(row: AssessmentSessionRow) -> str | None:
    deadline = manager_assessment_deadline_aware_utc(row)
    if deadline is None:
        return None
    return aware_utc_to_local_label(deadline)


def ensure_manager_access_token(db: Session, row: AssessmentSessionRow) -> str:
    existing = getattr(row, "manager_access_token", None)
    if existing and str(existing).strip():
        return str(existing).strip()
    row.manager_access_token = secrets.token_urlsafe(32)
    db.commit()
    db.refresh(row)
    return str(row.manager_access_token)


def _maybe_ensure_manager_access_token(db: Session, row: AssessmentSessionRow) -> str | None:
    status = getattr(row, "status", None) or ""
    phase = getattr(row, "phase", None) or ""
    if status == AssessmentSessionStatus.CANCELLED.value:
        return None
    if phase not in (SessionPhase.PART3.value, SessionPhase.COMPLETED.value, SessionPhase.REPORT.value):
        if not getattr(row, "manager_assessment_notified_at", None):
            return None
    return ensure_manager_access_token(db, row)


def _session_out(db: Session, row: AssessmentSessionRow) -> AssessmentSessionOut:
    ph = getattr(row, "phase", None) or SessionPhase.DRAFT.value
    sched = getattr(row, "docs_survey_scheduled_at", None)
    hr = docs_survey_hr_labels(
        docs_survey_scheduled_at=sched,
        docs_survey_reminder_30m_sent_at=getattr(row, "docs_survey_reminder_30m_sent_at", None),
        docs_survey_pd_consent_status=getattr(row, "docs_survey_pd_consent_status", None),
    )
    slot_ld, slot_lt = utc_naive_slot_to_local_date_time_strings(sched)
    manager_token = _maybe_ensure_manager_access_token(db, row)
    manager_url = None
    if manager_token:
        try:
            from skill_assessment.services.manager_assessment import (
                build_manager_assessment_absolute_url,
                build_manager_assessment_page_path,
            )

            manager_url = build_manager_assessment_absolute_url(db, row.id) or build_manager_assessment_page_path(manager_token)
        except Exception:
            manager_url = None
    return AssessmentSessionOut(
        id=row.id,
        client_id=row.client_id,
        employee_id=row.employee_id,
        status=AssessmentSessionStatus(row.status),
        phase=SessionPhase(ph),
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        docs_survey_pd_consent_status=getattr(row, "docs_survey_pd_consent_status", None),
        docs_survey_pd_consent_at=getattr(row, "docs_survey_pd_consent_at", None),
        docs_survey_scheduled_at=utc_naive_to_aware_utc(sched),
        docs_survey_readiness_answer=getattr(row, "docs_survey_readiness_answer", None),
        docs_survey_reminder_30m_sent_at=utc_naive_to_aware_utc(getattr(row, "docs_survey_reminder_30m_sent_at", None)),
        docs_survey_local_timezone=hr["docs_survey_local_timezone"],
        docs_survey_reminder_minutes_before=hr["docs_survey_reminder_minutes_before"],
        docs_survey_slot_local_label=hr["docs_survey_slot_local_label"],
        docs_survey_reminder_telegram_local_label=hr["docs_survey_reminder_telegram_local_label"],
        docs_survey_telegram_schedule_hint=hr["docs_survey_telegram_schedule_hint"],
        docs_survey_minutes_until_reminder=hr["docs_survey_minutes_until_reminder"],
        docs_survey_minutes_until_slot=hr["docs_survey_minutes_until_slot"],
        docs_survey_slot_local_date=slot_ld,
        docs_survey_slot_local_time=slot_lt,
        part1_docs_checklist_completed=is_docs_checklist_completed(row),
        part1_docs_checklist_token=getattr(row, "part1_docs_access_token", None),
        hr_no_show=is_hr_no_show(row, db),
        exam_status_label=latest_exam_status_label(db, row),
        manager_assessment_token=manager_token,
        manager_assessment_url=manager_url,
        manager_overall_comment=getattr(row, "manager_overall_comment", None),
        manager_assessment_deadline_at=manager_assessment_deadline_aware_utc(row),
        manager_assessment_deadline_label=manager_assessment_deadline_label(row),
        manager_assessment_notified_at=utc_naive_to_aware_utc(getattr(row, "manager_assessment_notified_at", None)),
    )


def _maybe_ensure_part1_docs_access_token(db: Session, row: AssessmentSessionRow) -> None:
    """Токен страницы чек-листа для сотрудника: создаём при активной сессии, если ещё нет."""
    if row.status != AssessmentSessionStatus.IN_PROGRESS.value:
        return
    if getattr(row, "part1_docs_access_token", None):
        return
    ensure_part1_docs_access_token(db, row)


def _evidence_to_json(notes: dict[EvidenceKind, str | None]) -> str | None:
    if not notes:
        return None
    d = {k.value: v for k, v in notes.items()}
    return json.dumps(d, ensure_ascii=False)


def _evidence_from_json(raw: str | None) -> dict[EvidenceKind, str | None]:
    if not raw:
        return {}
    data = json.loads(raw)
    out: dict[EvidenceKind, str | None] = {}
    for k, v in data.items():
        try:
            ek = EvidenceKind(k)
            out[ek] = v
        except ValueError:
            continue
    return out


def create_session(db: Session, body: SessionCreate) -> AssessmentSessionOut:
    if body.employee_id and str(body.employee_id).strip():
        eid = str(body.employee_id).strip()
        existing = db.scalars(
            select(AssessmentSessionRow)
            .where(
                AssessmentSessionRow.client_id == body.client_id,
                func.lower(AssessmentSessionRow.employee_id) == func.lower(eid),
                AssessmentSessionRow.status.in_(
                    (
                        AssessmentSessionStatus.DRAFT.value,
                        AssessmentSessionStatus.IN_PROGRESS.value,
                    )
                ),
            )
            .order_by(AssessmentSessionRow.created_at.desc())
        ).first()
        if existing is not None:
            _maybe_ensure_part1_docs_access_token(db, existing)
            db.refresh(existing)
            return _session_out(db, existing)
    sid = str(uuid.uuid4())
    row = AssessmentSessionRow(
        id=sid,
        client_id=body.client_id,
        employee_id=body.employee_id,
        status=AssessmentSessionStatus.DRAFT.value,
        phase=SessionPhase.DRAFT.value,
        started_at=None,
        completed_at=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _session_out(db, row)


def get_session(db: Session, session_id: str) -> AssessmentSessionOut:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    _maybe_ensure_part1_docs_access_token(db, row)
    db.refresh(row)
    return _session_out(db, row)


def list_sessions(
    db: Session,
    *,
    client_id: str | None = None,
    employee_id: str | None = None,
    phase: str | None = None,
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    pd_consent_status: str | None = None,
    docs_survey_slot_filter: str | None = None,
    q: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[AssessmentSessionOut], int]:
    """Фильтры + пагинация; ``pd_consent_status=__empty__`` — только без статуса согласия."""
    conditions: list = []
    if client_id:
        cid = client_id.strip()
        conditions.append(AssessmentSessionRow.client_id == cid)
    if employee_id:
        eid = employee_id.strip()
        # Ядро и SQLite могут хранить UUID в разном регистре — сравниваем без учёта регистра.
        conditions.append(func.lower(AssessmentSessionRow.employee_id) == func.lower(eid))
    if phase:
        conditions.append(AssessmentSessionRow.phase == phase)
    if status:
        conditions.append(AssessmentSessionRow.status == status)
    if created_from is not None:
        conditions.append(AssessmentSessionRow.created_at >= created_from)
    if created_to is not None:
        conditions.append(AssessmentSessionRow.created_at <= created_to)
    if pd_consent_status:
        if pd_consent_status == "__empty__":
            conditions.append(AssessmentSessionRow.docs_survey_pd_consent_status.is_(None))
        else:
            conditions.append(AssessmentSessionRow.docs_survey_pd_consent_status == pd_consent_status)
    if q and q.strip():
        qq = f"%{q.strip()}%"
        conditions.append(
            or_(
                AssessmentSessionRow.client_id.ilike(qq),
                AssessmentSessionRow.id.ilike(qq),
                AssessmentSessionRow.employee_id.ilike(qq),
            )
        )

    if docs_survey_slot_filter and str(docs_survey_slot_filter).strip():
        key = str(docs_survey_slot_filter).strip().lower()
        if key == "today":
            from skill_assessment.services.docs_survey_time import survey_slot_today_bounds_utc_naive

            a, b = survey_slot_today_bounds_utc_naive()
            conditions.append(AssessmentSessionRow.docs_survey_scheduled_at.isnot(None))
            conditions.append(AssessmentSessionRow.docs_survey_scheduled_at >= a)
            conditions.append(AssessmentSessionRow.docs_survey_scheduled_at < b)
        elif key == "upcoming":
            now_u = datetime.now(timezone.utc).replace(tzinfo=None)
            conditions.append(AssessmentSessionRow.docs_survey_scheduled_at.isnot(None))
            conditions.append(AssessmentSessionRow.docs_survey_scheduled_at >= now_u)
        elif key == "has_slot":
            conditions.append(AssessmentSessionRow.docs_survey_scheduled_at.isnot(None))

    count_stmt = select(func.count()).select_from(AssessmentSessionRow)
    list_stmt = select(AssessmentSessionRow).order_by(AssessmentSessionRow.created_at.desc())
    if conditions:
        count_stmt = count_stmt.where(and_(*conditions))
        list_stmt = list_stmt.where(and_(*conditions))
    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(list_stmt.offset(offset).limit(limit)).all()
    return [_session_out(db, r) for r in rows], total


def start_session(db: Session, session_id: str) -> AssessmentSessionOut:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.status != AssessmentSessionStatus.DRAFT.value:
        raise HTTPException(status_code=400, detail="session_not_draft")
    row.status = AssessmentSessionStatus.IN_PROGRESS.value
    row.phase = SessionPhase.PART1.value
    row.started_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    _maybe_ensure_part1_docs_access_token(db, row)
    db.refresh(row)
    return _session_out(db, row)


def cancel_session(db: Session, session_id: str, _body: SessionCancelBody | None = None) -> AssessmentSessionOut:
    """Отмена назначения: снимает «застрявшую» сессию, чтобы можно было создать новую."""
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.status == AssessmentSessionStatus.COMPLETED.value:
        raise HTTPException(status_code=400, detail="session_already_completed")
    if row.status == AssessmentSessionStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="session_already_cancelled")
    row.status = AssessmentSessionStatus.CANCELLED.value
    db.commit()
    db.refresh(row)
    return _session_out(db, row)


def delete_session(db: Session, session_id: str) -> None:
    """Полное удаление записи сессии из БД (HR): результаты и реплики Part1 удаляются каскадом."""
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    db.delete(row)
    db.commit()


def complete_session(db: Session, session_id: str) -> AssessmentSessionOut:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.status == AssessmentSessionStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="session_cancelled")
    row.status = AssessmentSessionStatus.COMPLETED.value
    row.phase = SessionPhase.COMPLETED.value
    row.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _session_out(db, row)


def set_docs_survey_slot_manual(db: Session, session_id: str, body: DocsSurveySlotManualUpdate) -> AssessmentSessionOut:
    """HR/examiner: вручную задать дату и время слота опроса по документам (перенос по звонку сотруднику)."""
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.status in (AssessmentSessionStatus.COMPLETED.value, AssessmentSessionStatus.CANCELLED.value):
        raise HTTPException(status_code=400, detail="session_not_editable_for_docs_slot")
    try:
        d = date_cls.fromisoformat(body.local_date.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="docs_survey_invalid_local_date") from exc
    tnorm = body.local_time.strip().replace(".", ":")
    parts = tnorm.split(":")
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail="docs_survey_invalid_local_time")
    try:
        h = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="docs_survey_invalid_local_time") from exc
    if not (0 <= h <= 23 and 0 <= minute <= 59):
        raise HTTPException(status_code=400, detail="docs_survey_invalid_local_time")
    row.docs_survey_scheduled_at = local_slot_to_utc_naive(d, h, minute)
    row.docs_survey_reminder_30m_sent_at = None
    row.docs_survey_readiness_answer = None
    db.commit()
    db.refresh(row)
    return _session_out(db, row)


def set_session_phase(db: Session, session_id: str, body: SessionPhaseUpdate) -> AssessmentSessionOut:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    old_phase = row.phase
    row.phase = body.phase.value
    db.commit()
    db.refresh(row)
    if old_phase == SessionPhase.PART1.value and row.phase == SessionPhase.PART2.value:
        try:
            from skill_assessment.services import part2_case as part2_case_svc

            part2_case_svc.send_part2_case_ready_notice(db, session_id)
        except Exception:
            _log.exception("assessment_service: part2 case Telegram notice failed after phase update %s", session_id)
    return _session_out(db, row)


def get_case_stub(db: Session, session_id: str, skill_id: str) -> CaseTextOut:
    session = db.get(AssessmentSessionRow, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if session.status == AssessmentSessionStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="session_cancelled")
    matrix_skill = session_competency_skill_map(
        db,
        session,
        include_inactive=True,
        ensure_result_skills=True,
    ).get(skill_id)
    if matrix_skill is not None:
        text = _template_case_text_title(matrix_skill.skill_title, matrix_skill.skill_code)
        return CaseTextOut(
            session_id=session_id,
            skill_id=matrix_skill.public_skill_id,
            skill_code=matrix_skill.skill_code,
            skill_title=matrix_skill.skill_title,
            text=text,
            source="template",
        )
    skill = db.get(SkillRow, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill_not_found")
    text = _template_case_text(skill)
    return CaseTextOut(
        session_id=session_id,
        skill_id=skill.id,
        skill_code=skill.code,
        skill_title=skill.title,
        text=text,
        source="template",
    )


def _template_case_text(skill: SkillRow) -> str:
    return _template_case_text_title(skill.title, skill.code)


def _template_case_text_title(skill_title: str, skill_code: str) -> str:
    return (
        f"**Ситуация (заглушка).** Навык в фокусе: «{skill_title}» ({skill_code}).\n\n"
        "Ключевой клиент требует дополнительные условия, угрожая уйти к конкуренту; "
        "маржа по сделке на грани минимума, регламент ограничивает уступки без согласования. "
        "До принятия решения остался один рабочий день.\n\n"
        "**Вопрос:** Какие шаги вы предпримете и как оформите решение?\n\n"
        "_Позже текст будет сгенерирован моделью по тем же входам, что в демо "
        "(контекст сессии, регламенты, KPI)._"
    )


def save_manager_ratings(db: Session, session_id: str, body: ManagerRatingsBulk) -> list[SkillAssessmentResultOut]:
    session_row = db.get(AssessmentSessionRow, session_id)
    if session_row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if session_row.status == AssessmentSessionStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="session_cancelled")
    matrix_skill_map = session_competency_skill_map(
        db,
        session_row,
        include_inactive=True,
        ensure_result_skills=True,
    )
    existing_rows = list(
        db.scalars(select(SkillAssessmentResultRow).where(SkillAssessmentResultRow.session_id == session_id)).all()
    )
    manager_rows_by_skill: dict[str, SkillAssessmentResultRow] = {}
    for row in existing_rows:
        notes = _evidence_from_json(row.evidence_json)
        if notes.get(EvidenceKind.MANAGER):
            manager_rows_by_skill[row.skill_id] = row
    touched: list[SkillAssessmentResultRow] = []
    for item in body.ratings:
        skill_id = item.skill_id
        matrix_skill = matrix_skill_map.get(skill_id)
        if matrix_skill is not None and matrix_skill.result_skill_id is not None:
            skill_id = matrix_skill.result_skill_id
        skill = db.get(SkillRow, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill_not_found")
        comment_stripped = (item.comment or "").strip()
        existing = manager_rows_by_skill.get(skill_id)
        if existing:
            notes = _evidence_from_json(existing.evidence_json)
            notes[EvidenceKind.MANAGER] = comment_stripped
            existing.level = int(item.level.value)
            existing.evidence_json = _evidence_to_json(notes)
            touched.append(existing)
        else:
            rid = str(uuid.uuid4())
            row = SkillAssessmentResultRow(
                id=rid,
                session_id=session_id,
                skill_id=skill_id,
                level=int(item.level.value),
                evidence_json=_evidence_to_json({EvidenceKind.MANAGER: comment_stripped}),
            )
            db.add(row)
            manager_rows_by_skill[skill_id] = row
            touched.append(row)
    session_row.phase = SessionPhase.PART3.value
    db.commit()
    for r in touched:
        db.refresh(r)
    return [_result_out(r) for r in touched]


def add_result(db: Session, session_id: str, body: SkillResultCreate) -> SkillAssessmentResultOut:
    session = db.get(AssessmentSessionRow, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if session.status == AssessmentSessionStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="session_cancelled")
    skill_id = body.skill_id
    matrix_skill = session_competency_skill_map(
        db,
        session,
        include_inactive=True,
        ensure_result_skills=True,
    ).get(skill_id)
    if matrix_skill is not None and matrix_skill.result_skill_id is not None:
        skill_id = matrix_skill.result_skill_id
    skill = db.get(SkillRow, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill_not_found")

    rid = str(uuid.uuid4())
    row = SkillAssessmentResultRow(
        id=rid,
        session_id=session_id,
        skill_id=skill_id,
        level=int(body.level.value),
        evidence_json=_evidence_to_json(body.evidence_notes),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _result_out(row)


def list_results(db: Session, session_id: str) -> list[SkillAssessmentResultOut]:
    session = db.get(AssessmentSessionRow, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    rows = db.scalars(
        select(SkillAssessmentResultRow).where(SkillAssessmentResultRow.session_id == session_id)
    ).all()
    return [_result_out(r) for r in rows]


def _result_out(row: SkillAssessmentResultRow) -> SkillAssessmentResultOut:
    return SkillAssessmentResultOut(
        id=row.id,
        session_id=row.session_id,
        skill_id=row.skill_id,
        level=ProficiencyLevel(row.level),
        evidence_notes=_evidence_from_json(row.evidence_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def list_domains(db: Session) -> list[SkillDomainOut]:
    rows = db.scalars(select(SkillDomainRow).order_by(SkillDomainRow.code)).all()
    return [SkillDomainOut(id=r.id, code=r.code, title=r.title) for r in rows]


def list_skills(db: Session, domain_id: str | None) -> list[SkillOut]:
    q = select(SkillRow)
    if domain_id:
        q = q.where(SkillRow.domain_id == domain_id)
    rows = db.scalars(q).all()
    return [SkillOut(id=r.id, domain_id=r.domain_id, code=r.code, title=r.title) for r in rows]
