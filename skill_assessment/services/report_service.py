# route: (service) | file: skill_assessment/services/report_service.py
"""Отчёт по сессии: JSON для фронта и HTML в духе demo/future-scenario."""

from __future__ import annotations

import html
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.domain.entities import EvidenceKind, Part1TurnRole
from skill_assessment.infrastructure.db_models import (
    AssessmentSessionRow,
    SessionPart1TurnRow,
    SkillAssessmentResultRow,
    SkillDomainRow,
    SkillRow,
)
from skill_assessment.integration.hr_core import employee_display_label, get_employee
from skill_assessment.schemas.api import ReportSkillRow, SessionReportOut
from skill_assessment.services.assessment_service import _evidence_from_json, _session_out
from skill_assessment.services.part1_service import turn_row_to_out


_LEVEL_TITLE_RU: dict[int, str] = {
    0: "Не демонстрирует",
    1: "Демонстрирует частично",
    2: "Демонстрирует правильно в типовой ситуации",
    3: "Демонстрирует правильно в сложной ситуации",
}


def _level_label(level: int) -> str:
    return _LEVEL_TITLE_RU.get(level, str(level))


def _latest_by_skill(rows: list[SkillAssessmentResultRow]) -> dict[str, SkillAssessmentResultRow]:
    by: dict[str, SkillAssessmentResultRow] = {}
    for r in sorted(rows, key=lambda x: (x.updated_at, x.created_at)):
        by[r.skill_id] = r
    return by


def build_session_report(db: Session, session_id: str) -> SessionReportOut:
    srow = db.get(AssessmentSessionRow, session_id)
    if srow is None:
        raise HTTPException(status_code=404, detail="session_not_found")

    res_rows = db.scalars(
        select(SkillAssessmentResultRow).where(SkillAssessmentResultRow.session_id == session_id)
    ).all()
    latest = _latest_by_skill(list(res_rows))

    report_rows: list[ReportSkillRow] = []
    for skill_id, rr in latest.items():
        sk = db.get(SkillRow, skill_id)
        if sk is None:
            continue
        dom = db.get(SkillDomainRow, sk.domain_id)
        domain_title = dom.title if dom else ""
        notes = _evidence_from_json(rr.evidence_json)
        lv = int(rr.level)
        report_rows.append(
            ReportSkillRow(
                skill_id=sk.id,
                skill_code=sk.code,
                skill_title=sk.title,
                domain_title=domain_title,
                level=lv,
                level_label_ru=_level_label(lv),
                part1_level=None,
                part2_level=lv if notes.get(EvidenceKind.CASE) else None,
                part3_level=lv if notes.get(EvidenceKind.MANAGER) else None,
                evidence_case=notes.get(EvidenceKind.CASE),
                evidence_manager=notes.get(EvidenceKind.MANAGER),
                evidence_metric=notes.get(EvidenceKind.METRIC),
            )
        )

    report_rows.sort(key=lambda x: (x.domain_title, x.skill_title))

    turn_rows = db.scalars(
        select(SessionPart1TurnRow)
        .where(SessionPart1TurnRow.session_id == session_id)
        .order_by(SessionPart1TurnRow.seq)
    ).all()
    part1_turns = [turn_row_to_out(r) for r in turn_rows]
    if part1_turns:
        p1_summary = f"Записано реплик: {len(part1_turns)} (текст после STT / реплики LLM)."
    else:
        p1_summary = "не проводилось (Part 1 — голос/STT позже)"

    emp_snap = get_employee(db, srow.client_id, srow.employee_id)
    emp_label = employee_display_label(emp_snap)

    return SessionReportOut(
        session=_session_out(srow),
        generated_at=datetime.utcnow(),
        employee_label=emp_label,
        part1_summary=p1_summary,
        part1_turns=part1_turns,
        part2_summary="кейс: см. evidence_case или заглушку Part 2",
        rows=report_rows,
    )


def render_session_report_html(rep: SessionReportOut) -> str:
    """HTML с теми же классами, что demo_future_scenario.html (layout.css)."""
    sess = rep.session
    title = "Отчёт по сессии оценки навыков"
    sub = (
        f"Сессия: <code>{html.escape(sess.id)}</code> · клиент: <strong>{html.escape(sess.client_id)}</strong> · "
        f"фаза: <strong>{html.escape(sess.phase.value)}</strong> · статус: <strong>{html.escape(sess.status.value)}</strong>"
    )

    blocks: list[str] = []
    if rep.part1_turns:
        dialog_parts: list[str] = []
        for t in rep.part1_turns:
            if t.role == Part1TurnRole.LLM:
                dialog_parts.append(
                    f'<div class="q"><strong>LLM:</strong> {html.escape(t.text)}</div>'
                )
            else:
                dialog_parts.append(
                    f'<div class="a"><strong>Ответ (после STT):</strong> {html.escape(t.text)}</div>'
                )
        dialog_html = '<div class="dialog">' + "".join(dialog_parts) + "</div>"
        blocks.append(
            f'<div class="block"><h2>Часть 1 — устное интервью (LLM)</h2>'
            f'<p class="meta" style="margin-top:0;">{html.escape(rep.part1_summary)}</p>'
            f"{dialog_html}</div>"
        )
    else:
        blocks.append(
            f'<div class="block"><h2>Часть 1 — устное интервью (LLM)</h2>'
            f'<p class="meta" style="margin-top:0;">{html.escape(rep.part1_summary)}</p></div>'
        )

    blocks.append(
        f'<div class="block"><h2>Часть 2 — кейс</h2>'
        f'<p class="meta" style="margin-top:0;">{html.escape(rep.part2_summary)}</p>'
        f'<p class="muted">Для полного текста кейса используйте '
        f'<code>GET /api/skill-assessment/sessions/{{id}}/case?skill_id=…</code> (заглушка или LLM).</p></div>'
    )

    tbody_mgr = []
    for r in rep.rows:
        mgr = r.evidence_manager or "—"
        tbody_mgr.append(
            "<tr>"
            f"<td>{html.escape(r.skill_title)}</td>"
            f"<td><strong>{r.part3_level if r.part3_level is not None else '—'}</strong></td>"
            f"<td>{html.escape(mgr)}</td>"
            "</tr>"
        )

    blocks.append(
        '<div class="block"><h2>Часть 3 — оценка руководителем</h2>'
        '<p class="meta" style="margin-top:0;">По навыкам с фиксацией evidence «manager».</p>'
        '<table><thead><tr><th>Навык</th><th>Оценка (0–3)</th><th>Комментарий / источник</th></tr></thead><tbody>'
        + ("".join(tbody_mgr) or "<tr><td colspan=\"3\">Нет данных</td></tr>")
        + "</tbody></table></div>"
    )

    tbody_sum = []
    for r in rep.rows:
        parts = [x for x in (r.part1_level, r.part2_level, r.part3_level) if x is not None]
        overall = f"{sum(parts) / len(parts):.1f}" if parts else "—"
        p1 = "—" if r.part1_level is None else str(r.part1_level)
        p2 = "—" if r.part2_level is None else str(r.part2_level)
        p3 = "—" if r.part3_level is None else str(r.part3_level)
        tbody_sum.append(
            "<tr>"
            f"<td>{html.escape(r.skill_title)}</td>"
            f"<td>{p1}</td><td>{p2}</td><td>{p3}</td>"
            f"<td><strong>{overall}</strong></td>"
            "</tr>"
        )

    blocks.append(
        '<div class="block"><h2>Сводка (иллюстрация весов)</h2>'
        '<table><thead><tr><th>Навык</th><th>Часть 1</th><th>Часть 2</th><th>Часть 3</th><th>Итог (среднее по заполненным)</th></tr></thead><tbody>'
        + ("".join(tbody_sum) or "<tr><td colspan=\"5\">Нет результатов</td></tr>")
        + '</tbody></table><p class="muted">Веса 0,3 / 0,4 / 0,3 из демо — только иллюстрация; при одном источнике берётся среднее по непустым частям.</p></div>'
    )

    body_inner = "\n".join(blocks)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="/static/shared/layout.css">
  <style>
    .wrap {{ max-width: 720px; padding: 2rem; }}
    .badge {{ display: inline-block; font-size: 0.72rem; padding: 0.2rem 0.5rem; border-radius: 4px; background: rgba(124,58,237,0.2); color: var(--accent); margin-right: 0.35rem; }}
    .block {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 1rem 1.25rem; margin-top: 1rem; }}
    .block h2 {{ font-size: 1rem; margin: 0 0 0.75rem 0; color: var(--accent); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-top: 0.5rem; }}
    th, td {{ border-bottom: 1px solid var(--border); padding: 0.45rem 0.5rem; text-align: left; }}
    th {{ color: var(--text-muted); font-weight: 500; }}
    .muted {{ font-size: 0.82rem; color: var(--text-muted); margin-top: 0.75rem; }}
    .dialog {{ font-size: 0.9rem; line-height: 1.55; }}
    .dialog .q {{ color: var(--text-muted); margin-top: 0.65rem; }}
    .dialog .a {{ margin: 0.25rem 0 0 1rem; border-left: 2px solid var(--accent); padding-left: 0.75rem; }}
  </style>
</head>
<body>
  <div class="app-layout">
    <aside class="sidebar">
      <div class="sidebar-top">
        <div class="sidebar-brand">Отчёт</div>
        <a href="/skill-assessment" class="sidebar-item">← Skill assessment</a>
        <a href="/docs" class="sidebar-item api-link">API</a>
      </div>
    </aside>
    <main class="main">
      <div class="content wrap">
        <p class="meta" style="margin:0;"><span class="badge">сессия</span> Данные из API skill-assessment</p>
        <h1 style="margin:0.5rem 0 0 0; font-size:1.35rem;">{html.escape(title)}</h1>
        <p class="meta">{sub}</p>
        <p class="muted">Сформировано: {html.escape(rep.generated_at.isoformat())}</p>
        {body_inner}
        <p class="meta"><a href="/skill-assessment">← Назад</a></p>
      </div>
    </main>
  </div>
</body>
</html>"""
