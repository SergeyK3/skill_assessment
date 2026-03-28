"""Part 3: персональная страница оценки руководителя и Telegram-уведомление."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
from skill_assessment.domain.entities import AssessmentSessionStatus, EvidenceKind, ProficiencyLevel, SessionPhase
from skill_assessment.env import load_plugin_env
from skill_assessment.infrastructure.db_models import (
    AssessmentSessionRow,
    SkillAssessmentResultRow,
)
from skill_assessment.integration.hr_core import employee_display_label, get_employee, get_examination_kpi_labels
from skill_assessment.schemas.api import (
    ManagerAssessmentPageOut,
    ManagerAssessmentSkillOut,
    ManagerRatingItem,
    ManagerAssessmentSubmitOut,
    ManagerRatingsBulk,
)
from skill_assessment.services import assessment_service as assessment_svc
from skill_assessment.services.exam_protocol_recipients import resolve_manager_telegram_chat_for_protocol
from skill_assessment.services.examination_question_plan import compose_examination_question_plan
from skill_assessment.services.session_competency_matrix import (
    ensure_mirrored_skill,
    find_mirrored_skill,
    list_session_competency_skills,
    session_competency_skill_map,
)

MANAGER_ASSESSMENT_UI_PATH = "/api/skill-assessment/ui/manager-assessment"


def _stored_manager_comment_for_ui(raw: str | None) -> str | None:
    """Текст комментария для формы: без устаревших служебных заглушек."""
    if raw is None:
        return None
    t = str(raw).strip()
    if not t:
        return None
    if t in ("Оценка руководителя", "Оценка руководителя."):
        return None
    for suffix in (" (Part 3)", "(Part 3)"):
        t = t.replace(suffix, "").strip()
    if t in ("Оценка руководителя", "Оценка руководителя."):
        return None
    return t or None


def build_manager_assessment_page_path(token: str) -> str:
    return MANAGER_ASSESSMENT_UI_PATH + "?" + urlencode({"token": token})


def build_manager_assessment_absolute_url(db: Session, session_id: str) -> str:
    base = (os.getenv("SKILL_ASSESSMENT_PUBLIC_BASE_URL") or "http://127.0.0.1:8000").strip().rstrip("/")
    row = _resolve_session_row(db, session_id)
    token = assessment_svc.ensure_manager_access_token(db, row)
    return base + build_manager_assessment_page_path(token)


def get_session_row_by_manager_token(db: Session, token: str) -> AssessmentSessionRow | None:
    t = (token or "").strip()
    if len(t) < 16:
        return None
    return db.scalar(select(AssessmentSessionRow).where(AssessmentSessionRow.manager_access_token == t))


def _resolve_session_row(db: Session, session_id: str) -> AssessmentSessionRow:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    return row


def _row_for_public_token(db: Session, token: str) -> AssessmentSessionRow:
    row = get_session_row_by_manager_token(db, token)
    if row is None:
        raise HTTPException(status_code=404, detail="manager_assessment_token_invalid")
    if row.status == AssessmentSessionStatus.CANCELLED.value:
        raise HTTPException(status_code=404, detail="session_cancelled")
    if row.phase not in (SessionPhase.PART3.value, SessionPhase.COMPLETED.value, SessionPhase.REPORT.value):
        raise HTTPException(status_code=400, detail="manager_assessment_not_ready")
    return row


def _kpi_summary(db: Session, row: AssessmentSessionRow) -> str:
    if row.employee_id:
        plan = compose_examination_question_plan(db, row.client_id, row.employee_id)
        if plan:
            for item in plan:
                if "kpi" in item.lower():
                    return item
    return "Используйте KPI должности и результаты кейсов как дополнительный ориентир при выставлении оценки."


def _kpi_lines(db: Session, row: AssessmentSessionRow) -> list[str]:
    labels = get_examination_kpi_labels(db, row.client_id, row.employee_id or "")
    if labels:
        return labels[:5]
    summary = _kpi_summary(db, row)
    return [summary] if summary else []


def _part2_summary(row: AssessmentSessionRow) -> str:
    payload = assessment_svc._parse_part2_payload(row)
    if not payload:
        return "Кейсы ещё не завершены."
    case_count = int(payload.get("case_count") or 0)
    if bool(payload.get("completed")):
        solved = int(payload.get("solved_cases") or 0)
        pct = int(payload.get("overall_pct") or 0)
        return f"Кейсы: {solved}/{case_count} = {pct}%."
    minutes = int(payload.get("allotted_minutes") or 0)
    return f"Кейсы назначены: {case_count}, время на решение {minutes} мин."


def _skills_with_current_manager_levels(
    db: Session, session_row: AssessmentSessionRow, kpi_hint: str
) -> list[ManagerAssessmentSkillOut]:
    matrix_skills = list_session_competency_skills(db, session_row, include_inactive=True, ensure_result_skills=False)
    result_rows = list(
        db.scalars(select(SkillAssessmentResultRow).where(SkillAssessmentResultRow.session_id == session_row.id)).all()
    )
    latest_by_skill: dict[str, SkillAssessmentResultRow] = {}
    for row in sorted(result_rows, key=lambda x: (x.updated_at, x.created_at)):
        latest_by_skill[row.skill_id] = row
    out: list[ManagerAssessmentSkillOut] = []
    for item in matrix_skills:
        current_level: ProficiencyLevel | None = None
        res = latest_by_skill.get(item.result_skill_id) if item.result_skill_id is not None else None
        manager_comment: str | None = None
        if res is not None:
            notes = assessment_svc._evidence_from_json(res.evidence_json)
            if EvidenceKind.MANAGER in notes:
                try:
                    current_level = ProficiencyLevel(int(res.level))
                except ValueError:
                    current_level = None
                manager_comment = _stored_manager_comment_for_ui(notes.get(EvidenceKind.MANAGER))
        out.append(
            ManagerAssessmentSkillOut(
                skill_id=item.public_skill_id,
                skill_code=item.skill_code,
                skill_title=item.skill_title,
                is_active=item.is_active,
                current_level=current_level,
                manager_comment=manager_comment,
                kpi_hint=kpi_hint,
            )
        )
    return out


def _translate_matrix_ratings_to_skill_ratings(
    db: Session, session_row: AssessmentSessionRow, body: ManagerRatingsBulk
) -> ManagerRatingsBulk:
    matrix_skills = session_competency_skill_map(
        db,
        session_row,
        include_inactive=True,
        ensure_result_skills=True,
    )
    if not matrix_skills:
        raise HTTPException(status_code=400, detail="manager_assessment_skills_not_configured")
    seen_skill_ids: set[str] = set()
    translated: list[ManagerRatingItem] = []
    for item in body.ratings:
        if item.skill_id in seen_skill_ids:
            raise HTTPException(status_code=400, detail="manager_assessment_skill_duplicate")
        seen_skill_ids.add(item.skill_id)
        skill_ref = matrix_skills.get(item.skill_id)
        if skill_ref is None or skill_ref.result_skill_id is None:
            raise HTTPException(status_code=400, detail="manager_assessment_skill_not_allowed")
        translated.append(
            ManagerRatingItem(skill_id=skill_ref.result_skill_id, level=item.level, comment=item.comment)
        )
    missing_required = [
        skill_ref.public_skill_id
        for skill_ref in matrix_skills.values()
        if skill_ref.is_active and skill_ref.public_skill_id not in seen_skill_ids
    ]
    if missing_required:
        raise HTTPException(status_code=400, detail="manager_assessment_required_skills_missing")
    return ManagerRatingsBulk(ratings=translated)


def get_manager_assessment_page(db: Session, token: str) -> ManagerAssessmentPageOut:
    row = _row_for_public_token(db, token)
    emp = get_employee(db, row.client_id, row.employee_id)
    employee_label = employee_display_label(emp) or row.employee_id or "—"
    position = (emp.position_label or "").strip() if emp and emp.position_label else "—"
    kpi_hint = _kpi_summary(db, row)
    skills = _skills_with_current_manager_levels(db, row, kpi_hint)
    return ManagerAssessmentPageOut(
        session_id=row.id,
        employee_label=employee_label,
        employee_position_label=position,
        deadline_at=assessment_svc.manager_assessment_deadline_aware_utc(row),
        deadline_label=assessment_svc.manager_assessment_deadline_label(row),
        part2_summary=_part2_summary(row),
        kpi_summary=kpi_hint,
        can_submit=row.status != AssessmentSessionStatus.COMPLETED.value and bool(skills),
        skills=skills,
    )


def submit_manager_assessment_by_token(db: Session, token: str, body: ManagerRatingsBulk) -> ManagerAssessmentSubmitOut:
    row = _row_for_public_token(db, token)
    if row.status == AssessmentSessionStatus.COMPLETED.value:
        raise HTTPException(status_code=400, detail="manager_assessment_already_completed")
    translated = _translate_matrix_ratings_to_skill_ratings(db, row, body)
    saved = assessment_svc.save_manager_ratings(db, row.id, translated)
    fresh = assessment_svc.complete_session(db, row.id)
    try:
        send_employee_protocol_updated_notice(db, row.id)
    except Exception:
        # Уведомление сотруднику не должно ломать сохранение оценки руководителя.
        pass
    return ManagerAssessmentSubmitOut(
        saved_count=len(saved),
        status=fresh.status,
        phase=fresh.phase,
        completed_at=fresh.completed_at,
    )


def send_manager_assessment_ready_notice(db: Session, session_id: str) -> dict[str, object]:
    load_plugin_env(override=False)
    row = _resolve_session_row(db, session_id)
    if row.status == AssessmentSessionStatus.CANCELLED.value:
        return {"sent": False, "reason": "session_cancelled"}
    if getattr(row, "manager_assessment_notified_at", None):
        return {"sent": False, "reason": "already_notified"}
    chat_id = resolve_manager_telegram_chat_for_protocol(db, row.client_id, row.employee_id or "")
    if not chat_id:
        return {"sent": False, "reason": "no_manager_chat_id"}
    use_mock = (os.getenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND") or "").strip().lower() == "mock"
    token_env = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not use_mock and (not token_env or len(token_env) < 10):
        return {"sent": False, "reason": "no_bot_token"}

    emp = get_employee(db, row.client_id, row.employee_id)
    employee_label = employee_display_label(emp) or row.employee_id or "—"
    position = (emp.position_label or "").strip() if emp and emp.position_label else "—"
    url = build_manager_assessment_absolute_url(db, row.id)
    deadline = assessment_svc.manager_assessment_deadline_label(row) or "не задан"
    kpi_lines = _kpi_lines(db, row)
    lines = [
        f"Нужно оценить сотрудника: {employee_label}",
        f"Должность: {position}",
        "Этап: оценка руководителем",
        f"Дедлайн оценки: {deadline}",
    ]
    if kpi_lines:
        lines.append("KPI:")
        lines.extend([f"- {item}" for item in kpi_lines])
    lines.extend(
        [
        f"Открыть страницу: {url}",
        ]
    )
    send_token = token_env if token_env else "mock_token_for_tests"
    outbound = get_telegram_outbound()
    result = outbound.send_message(
        token=send_token,
        chat_id=str(chat_id).strip(),
        text="\n".join(lines),
        reply_markup=None,
    )
    if not result.ok:
        return {"sent": False, "reason": result.description or "send_failed", "chat_id": chat_id}
    row.manager_assessment_notified_at = datetime.now(timezone.utc).replace(tzinfo=None)
    assessment_svc.ensure_manager_access_token(db, row)
    db.commit()
    db.refresh(row)
    return {"sent": True, "chat_id": chat_id, "url": url}


def send_employee_protocol_updated_notice(db: Session, session_id: str) -> dict[str, object]:
    """После Part 3 отправить сотруднику ссылку на обновлённый общий протокол."""
    load_plugin_env(override=False)
    row = _resolve_session_row(db, session_id)
    if row.status == AssessmentSessionStatus.CANCELLED.value:
        return {"sent": False, "reason": "session_cancelled"}
    use_mock = (os.getenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND") or "").strip().lower() == "mock"
    token_env = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not use_mock and (not token_env or len(token_env) < 10):
        return {"sent": False, "reason": "no_bot_token"}

    from skill_assessment.services import part2_case as part2_case_svc

    chat_id = part2_case_svc._resolve_case_chat_id(db, row)
    if not chat_id:
        return {"sent": False, "reason": "no_chat_id"}

    emp = get_employee(db, row.client_id, row.employee_id)
    employee_label = employee_display_label(emp) or row.employee_id or "коллега"
    token = row.part1_docs_access_token or ""
    report_url = part2_case_svc.build_public_report_absolute_url(db, row.id)
    report_path = part2_case_svc.build_public_report_path(token) if token else None
    text = "\n".join(
        [
            f"Здравствуйте, {employee_label}!",
            "",
            "Оценка руководителя добавлена в общий протокол.",
            "Теперь в протоколе собраны: опрос, кейсы и оценка руководителя.",
            "",
            "Открыть общий протокол:",
            report_url if report_url else (report_path or "ссылка недоступна"),
        ]
    )
    outbound = get_telegram_outbound()
    result = outbound.send_message(
        token=token_env if token_env else "mock_token_for_tests",
        chat_id=str(chat_id).strip(),
        text=text,
        reply_markup=None,
    )
    return {
        "sent": bool(result.ok),
        "reason": result.description,
        "http_status": result.http_status,
        "chat_id": chat_id,
    }
