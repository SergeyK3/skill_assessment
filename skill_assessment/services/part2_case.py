"""Part 2: генерация набора кейсов, ответы сотрудника и оценка ИИ."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
from skill_assessment.domain.entities import AssessmentSessionStatus, EvidenceKind, SessionPhase
from skill_assessment.infrastructure.db_models import (
    AssessmentSessionRow,
    SkillAssessmentResultRow,
    SkillRow,
)
from skill_assessment.integration.hr_core import (
    get_assessment_case_count,
    employee_greeting_label,
    get_employee,
    get_examination_regulation_reference_text,
)
from skill_assessment.schemas.api import (
    CaseTextOut,
    Part2CaseAnswerIn,
    Part2CaseItemOut,
    Part2AdditionalCasesRequest,
    Part2CasesHrOut,
    Part2CasesPublicOut,
    Part2LlmCostsOut,
    Part2LlmCostStepOut,
    Part2AiCommissionConsensusOut,
    Part2SkillEvaluationOut,
    Part2CasesSubmit,
    Part2SkillRefOut,
)
from skill_assessment.services import assessment_service as assessment_svc
from skill_assessment.services import examination_service as examination_svc
from skill_assessment.services import llm_costs as llm_costs_svc
from skill_assessment.services import manager_assessment as manager_assessment_svc
from skill_assessment.services import part1_docs_checklist as part1_docs_svc
from skill_assessment.services.public_url import skill_assessment_public_base_url_for_device_links
from skill_assessment.services.session_competency_matrix import (
    SessionCompetencySkill,
    list_session_competency_skills,
    session_competency_skill_map,
)

_log = logging.getLogger(__name__)

PART2_CASE_EMPLOYEE_UI_PATH = "/api/skill-assessment/ui/part2-case"
_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
_CASE_SOURCE_TEMPLATE = "template"
_CASE_SOURCE_LLM = "llm"
_PART2_CASES_VERSION = "v3"
_STEP_CASE_GENERATION = "case_generation"
_STEP_CASE_EVALUATION = "case_evaluation"
_STEP_SKILL_EVALUATION = "skill_evaluation"
_STEP_AI_COMMISSION = "ai_commission"
_ANSWER_LABELS = {
    "yes": "владеет уверенно",
    "partial": "закрыто частично",
    "no": "есть пробел",
}


def _clean_skill_label_for_telegram(raw: str | None) -> str:
    t = str(raw or "").strip()
    if not t:
        return ""
    # Убираем технические id в хвостах вида "(C_xxx)", "(c_xxx)".
    t = re.sub(r"\(\s*[cC]_[^)]+\)", "", t).strip()
    t = re.sub(r"\s{2,}", " ", t)
    return t


def _strip_technical_skill_ids(text: str | None) -> str:
    t = str(text or "")
    if not t:
        return ""
    t = re.sub(r"\(\s*[cC]_[^)]+\)", "", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


def _clean_case_text_for_telegram(text: str | None) -> str:
    """
    Убираем служебные/методические строки из кейса для Telegram.
    В чате сотруднику нужен только сам сценарий кейса.
    """
    raw = _strip_technical_skill_ids(text)
    if not raw:
        return ""
    drop_prefixes = (
        "заголовок:",
        "навыки в фокусе:",
        "навык в фокусе:",
        "роль:",
        "должность:",
    )
    kept: list[str] = []
    for line in raw.splitlines():
        ln = line.strip()
        if not ln:
            kept.append("")
            continue
        low = ln.lower()
        if low.startswith(drop_prefixes):
            continue
        if low == "ситуация:":
            continue
        if low.startswith("вы выполняете роль"):
            pos = low.find("возникла")
            if pos >= 0:
                kept.append(ln[pos:].strip())
            continue
        kept.append(ln)
    text_out = "\n".join(kept)
    text_out = re.sub(r"\n{3,}", "\n\n", text_out)
    text_out = re.sub(
        r"(?is)где нужно не только показать основной навык\s+«[^»]+»,?\s*но и одновременно проявить связанные навыки:\s*[^.]+\.?",
        "в которой нужно принять решение в условиях ограниченного времени.",
        text_out,
    )
    text_out = re.sub(r"(?is)основн(?:ой|ые)\s+навык[аи]?\s+«[^»]+»", "", text_out)
    text_out = re.sub(r"(?is)связанн(?:ые|ый)\s+навык[аи]?:\s*[^.]+\.?", "", text_out)
    text_out = re.sub(r"(?is)навык[аи]?\s+«[^»]+»", "", text_out)
    text_out = re.sub(r"(?im)^ситуация:\s*", "", text_out).strip()
    text_out = re.sub(r"\s{2,}", " ", text_out)
    return text_out.strip()


def _telegram_case_message(case_idx: int, total: int, case_text: str | None) -> str:
    header = f"Кейс {case_idx} из {total}."
    body = _clean_case_text_for_telegram(case_text)
    if body:
        return f"{header}\n\n{body}"
    return header


def _build_part2_case_ready_message_queue(payload: dict[str, Any]) -> list[str]:
    cases_list = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    n_cases = len(cases_list)
    intro = "Этап 2: решение кейсов."
    first_case_text = str(cases_list[0].get("text") or "") if cases_list else ""
    first_case_block = _telegram_case_message(1, n_cases, first_case_text) if n_cases else ""
    queue: list[str] = [intro]
    if first_case_block:
        queue.extend(_chunk_text_for_telegram(first_case_block))
    return [item for item in queue if (item or "").strip()]


def prepare_part2_case_ready_notice(
    db: Session, session_id: str, skill_id: str | None = None, *, preferred_chat_id: str | None = None
) -> dict[str, Any]:
    row = _resolve_session_row(db, session_id)
    chat_id = (preferred_chat_id or "").strip() or _resolve_case_chat_id(db, row)
    bundle = get_session_cases(db, row.id)
    case = get_session_case(db, row.id, skill_id)
    if not chat_id:
        return {"sent": False, "reason": "no_chat_id", "case": case, "bundle": bundle, "messages": []}

    db.refresh(row)
    payload = _parse_cases_payload(row.part2_cases_json)
    cases_list = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    n_cases = len(cases_list)
    message_queue = _build_part2_case_ready_message_queue(payload)
    if n_cases > 0:
        payload[_TELEGRAM_ANSWER_FLOW_KEY] = {"awaiting_index": 0, "answers": {}}
        row.part2_cases_json = _dump_cases_payload(payload)
        db.commit()
        db.refresh(row)
    return {
        "sent": bool(message_queue),
        "reason": None,
        "http_status": None,
        "case": case,
        "bundle": bundle,
        "chat_id": chat_id,
        "telegram_messages_sent": len(message_queue),
        "messages": message_queue,
    }
_PASS_PCT_MAP = {
    1: {0: 0, 1: 100},
    2: {0: 0, 1: 67, 2: 100},
    3: {0: 0, 1: 67, 2: 84, 3: 100},
}


def _openai_api_key() -> str:
    return (os.getenv("SKILL_ASSESSMENT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()


def _case_model_name() -> str:
    raw = (os.getenv("SKILL_ASSESSMENT_CASE_LLM_MODEL") or "").strip()
    return raw or "gpt-4.1-mini"


def _case_eval_model_name() -> str:
    raw = (os.getenv("SKILL_ASSESSMENT_CASE_EVAL_LLM_MODEL") or "").strip()
    return raw or _case_model_name()


def _default_case_count() -> int:
    raw = (os.getenv("SKILL_ASSESSMENT_CASE_COUNT_DEFAULT") or "").strip()
    try:
        n = int(raw)
    except Exception:
        n = 2
    return max(1, min(3, n))


def _case_minutes_per_item() -> int:
    raw = (os.getenv("SKILL_ASSESSMENT_CASE_MINUTES_PER_ITEM") or "").strip()
    try:
        n = int(raw)
    except Exception:
        n = 10
    return max(1, min(60, n))


def _resolve_session_row(db: Session, session_id: str) -> AssessmentSessionRow:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.status == "cancelled":
        raise HTTPException(status_code=400, detail="session_cancelled")
    return row


def _resolve_public_session_row(db: Session, token: str) -> AssessmentSessionRow:
    row = part1_docs_svc.get_session_row_by_part1_docs_token(db, token)
    if row is None:
        raise HTTPException(status_code=404, detail="part2_case_token_invalid")
    if row.status == "cancelled":
        raise HTTPException(status_code=404, detail="session_cancelled")
    if row.status == "draft":
        raise HTTPException(status_code=400, detail="session_not_started")
    return row


def _resolve_skill(db: Session, skill_id: str | None) -> SkillRow:
    if skill_id and str(skill_id).strip():
        row = db.get(SkillRow, str(skill_id).strip())
        if row is None:
            raise HTTPException(status_code=404, detail="skill_not_found")
        return row
    row = db.scalars(select(SkillRow).order_by(SkillRow.created_at.asc(), SkillRow.code.asc())).first()
    if row is None:
        raise HTTPException(status_code=404, detail="skill_not_found")
    return row


def _parse_cases_payload(raw: str | None) -> dict[str, Any]:
    if not raw or not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _dump_cases_payload(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


_TELEGRAM_ANSWER_FLOW_KEY = "telegram_answer_flow"


def _chunk_text_for_telegram(text: str, max_len: int = 3800) -> list[str]:
    """Telegram лимит 4096 символов; режем по строкам, затем по длине."""
    s = (text or "").strip()
    if not s:
        return []
    out: list[str] = []
    while s:
        if len(s) <= max_len:
            out.append(s)
            break
        cut = s.rfind("\n\n", 0, max_len)
        if cut < max_len // 2:
            cut = s.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        chunk = s[:cut].rstrip()
        if not chunk:
            chunk = s[:max_len]
            s = s[max_len:].lstrip()
        else:
            s = s[cut:].lstrip()
        out.append(chunk)
    return out


def _skill_ref(skill: SessionCompetencySkill) -> dict[str, str]:
    return {
        "skill_id": str(skill.public_skill_id),
        "skill_code": str(skill.skill_code),
        "skill_title": str(skill.skill_title),
    }


def _skill_ref_from_case(case: dict[str, Any]) -> dict[str, str]:
    return {
        "skill_id": str(case.get("skill_id") or ""),
        "skill_code": str(case.get("skill_code") or ""),
        "skill_title": str(case.get("skill_title") or ""),
    }


def _pct_for_level(level: int | None) -> int | None:
    if level is None:
        return None
    return {0: 0, 1: 35, 2: 75, 3: 100}.get(int(level), None)


def _level_from_pct(pct: int | None) -> int | None:
    if pct is None:
        return None
    value = max(0, min(100, int(pct)))
    if value >= 90:
        return 3
    if value >= 65:
        return 2
    if value >= 30:
        return 1
    return 0


def _round_money(value: Any, digits: int = 4) -> float:
    try:
        return round(float(value or 0.0), digits)
    except Exception:
        return 0.0


def _default_llm_costs() -> dict[str, Any]:
    return llm_costs_svc.empty_costs()


def _normalize_llm_costs(costs: Any) -> dict[str, Any]:
    if not isinstance(costs, dict):
        return _default_llm_costs()
    steps_raw = costs.get("steps")
    steps: list[dict[str, Any]] = []
    if isinstance(steps_raw, list):
        for item in steps_raw:
            if not isinstance(item, dict):
                continue
            step = str(item.get("step") or "").strip()
            if not step:
                continue
            steps.append(
                {
                    "step": step,
                    "label": str(item.get("label") or llm_costs_svc.step_label(step)),
                    "model": (str(item.get("model")).strip() if item.get("model") is not None else None),
                    "calls": max(0, int(item.get("calls") or 0)),
                    "usage_missing_calls": max(0, int(item.get("usage_missing_calls") or 0)),
                    "input_tokens": max(0, int(item.get("input_tokens") or 0)),
                    "output_tokens": max(0, int(item.get("output_tokens") or 0)),
                    "total_tokens": max(0, int(item.get("total_tokens") or 0)),
                    "cost_usd": _round_money(item.get("cost_usd"), 6),
                    "cost_rub": _round_money(item.get("cost_rub"), 4),
                }
            )
    out = {
        "currency": "USD/RUB",
        "usd_to_rub_rate": _round_money(costs.get("usd_to_rub_rate"), 4) or llm_costs_svc.usd_to_rub_rate(),
        "steps": steps,
        "total_cost_usd": _round_money(costs.get("total_cost_usd"), 6),
        "total_cost_rub": _round_money(costs.get("total_cost_rub"), 4),
        "total_input_tokens": max(0, int(costs.get("total_input_tokens") or 0)),
        "total_output_tokens": max(0, int(costs.get("total_output_tokens") or 0)),
        "total_tokens": max(0, int(costs.get("total_tokens") or 0)),
    }
    return llm_costs_svc.recompute_totals(out)


def _normalize_skill_evaluation(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    skill_id = str(item.get("skill_id") or "").strip()
    if not skill_id:
        return None
    pct_value = item.get("pct_0_100")
    pct = None if pct_value is None else max(0, min(100, int(pct_value)))
    level_value = item.get("level_0_3")
    level = None if level_value is None else max(0, min(3, int(level_value)))
    if level is None and pct is not None:
        level = _level_from_pct(pct)
    if pct is None and level is not None:
        pct = _pct_for_level(level)
    return {
        "skill_id": skill_id,
        "skill_code": str(item.get("skill_code") or ""),
        "skill_title": str(item.get("skill_title") or ""),
        "level_0_3": level,
        "pct_0_100": pct,
        "evidence": (str(item.get("evidence")).strip() if item.get("evidence") is not None else None),
        "gaps": (str(item.get("gaps")).strip() if item.get("gaps") is not None else None),
    }


def _default_ai_commission_consensus(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not bool(payload.get("completed")):
        return None
    overall_pct = int(payload.get("overall_pct") or 0)
    case_count = max(1, int(payload.get("case_count") or 1))
    solved = max(0, min(case_count, int(payload.get("solved_cases") or 0)))
    level = _level_from_pct(overall_pct)
    return {
        "overall_level_0_3": level,
        "overall_pct_0_100": overall_pct,
        "summary": f"По кейсам решено {solved} из {case_count}; интегральная оценка блока — {overall_pct}%.",
        "recommendation": (
            "Можно переходить к верификации руководителем."
            if overall_pct >= 65
            else "Нужна дополнительная проверка руководителем и разбор пробелов."
        ),
        "strengths": [],
        "risks": [],
    }


def _normalize_ai_commission_consensus(
    consensus: Any,
    *,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(consensus, dict):
        return _default_ai_commission_consensus(payload)
    pct_value = consensus.get("overall_pct_0_100")
    pct = None if pct_value is None else max(0, min(100, int(pct_value)))
    level_value = consensus.get("overall_level_0_3")
    level = None if level_value is None else max(0, min(3, int(level_value)))
    if level is None and pct is not None:
        level = _level_from_pct(pct)
    if pct is None and level is not None:
        pct = _pct_for_level(level)
    return {
        "overall_level_0_3": level,
        "overall_pct_0_100": pct,
        "summary": (str(consensus.get("summary")).strip() if consensus.get("summary") is not None else None),
        "recommendation": (
            str(consensus.get("recommendation")).strip() if consensus.get("recommendation") is not None else None
        ),
        "strengths": [str(x).strip() for x in (consensus.get("strengths") or []) if str(x).strip()],
        "risks": [str(x).strip() for x in (consensus.get("risks") or []) if str(x).strip()],
    }


def _extract_json_object(text: str) -> dict[str, Any] | None:
    s = (text or "").strip()
    if not s:
        return None
    try:
        data = json.loads(s)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    a = s.find("{")
    b = s.rfind("}")
    if a >= 0 and b > a:
        try:
            data = json.loads(s[a : b + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def build_part2_case_employee_page_path(token: str, *, skill_id: str | None = None) -> str:
    qs: dict[str, str] = {"token": token}
    if skill_id and str(skill_id).strip():
        qs["skill_id"] = str(skill_id).strip()
    return PART2_CASE_EMPLOYEE_UI_PATH + "?" + urlencode(qs)


def build_part2_case_employee_page_absolute_url(
    db: Session,
    session_id: str,
    *,
    skill_id: str | None = None,
) -> str | None:
    base = skill_assessment_public_base_url_for_device_links()
    if not base:
        return None
    row = _resolve_session_row(db, session_id)
    token = part1_docs_svc.ensure_part1_docs_access_token(db, row)
    return base + build_part2_case_employee_page_path(token, skill_id=skill_id)


def build_public_report_path(token: str) -> str:
    return "/api/skill-assessment/public/report/html?" + urlencode({"token": token})


def build_public_report_absolute_url(db: Session, session_id: str) -> str | None:
    base = skill_assessment_public_base_url_for_device_links()
    if not base:
        return None
    row = _resolve_session_row(db, session_id)
    token = part1_docs_svc.ensure_part1_docs_access_token(db, row)
    return base + build_public_report_path(token)


def _focus_topics_for_session(db: Session, row: AssessmentSessionRow) -> list[str]:
    checklist = part1_docs_svc.get_part1_docs_checklist(db, row.id)
    topics: list[str] = []
    for q in checklist.questions:
        answer = (checklist.answers.get(q.id) or "").strip().lower()
        if answer in ("partial", "no"):
            topics.append(f"{q.text} ({_ANSWER_LABELS.get(answer, answer)})")
    if topics:
        return topics[:3]
    fallback = [q.text for q in checklist.questions[:2]]
    return fallback or ["Применение внутренних регламентов в рабочей ситуации"]


def _all_skills(db: Session, row: AssessmentSessionRow) -> list[SessionCompetencySkill]:
    return list_session_competency_skills(db, row, include_inactive=False, ensure_result_skills=True)


def _recommended_case_count(skill_count: int, preferred: int | None = None) -> int:
    if skill_count <= 0:
        return 0
    raw = preferred if preferred is not None else _default_case_count()
    return max(1, min(3, int(raw), int(skill_count)))


def _requested_case_count(db: Session, row: AssessmentSessionRow, skill_count: int) -> int:
    requested = get_assessment_case_count(db, row.client_id, row.employee_id)
    return _recommended_case_count(skill_count, requested)


def _group_skills_for_cases(skills: list[SessionCompetencySkill], case_count: int) -> list[list[SessionCompetencySkill]]:
    count = _recommended_case_count(len(skills), case_count)
    if count <= 0:
        return []
    ordered = list(skills)
    if count == 1:
        return [ordered]
    if count == 2:
        # Приоритетный порядок без «перемешивания»: кейс 1 — первые 2 навыка, кейс 2 — следующие.
        g1 = ordered[:2]
        g2 = ordered[2:4]
        rest = ordered[4:]
        if rest:
            g2.extend(rest)
        out = [g for g in (g1, g2) if g]
        return out if out else [ordered]

    # Для 3 кейсов — последовательные «блоки» по приоритету (а не round-robin).
    chunk = max(1, (len(ordered) + count - 1) // count)
    groups = [ordered[i : i + chunk] for i in range(0, len(ordered), chunk)]
    return [g for g in groups if g][:count]


def _position_label(row: AssessmentSessionRow, db: Session) -> str:
    emp = get_employee(db, row.client_id, row.employee_id)
    if emp is not None and emp.position_label and str(emp.position_label).strip():
        return str(emp.position_label).strip()
    return "сотрудник"


def _regulation_snippet(db: Session, row: AssessmentSessionRow) -> str:
    raw = get_examination_regulation_reference_text(db, row.client_id, row.employee_id or "")
    if not raw:
        return ""
    text = " ".join(str(raw).split())
    return text[:1800]


def _openai_text_response(
    *,
    model: str,
    prompt: str,
    max_output_tokens: int,
    log_label: str,
) -> tuple[dict[str, Any] | None, str]:
    api_key = _openai_api_key()
    if not api_key:
        return None, ""
    try:
        with httpx.Client(timeout=90.0) as client:
            r = client.post(
                _OPENAI_RESPONSES_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "input": prompt,
                    "max_output_tokens": max_output_tokens,
                },
            )
        if r.status_code >= 400:
            _log.warning("part2_case: OpenAI %s HTTP %s: %s", log_label, r.status_code, r.text[:400])
            return None, ""
        payload = r.json()
        return payload, str(payload.get("output_text") or "").strip()
    except Exception:
        _log.exception("part2_case: OpenAI %s failed", log_label)
        return None, ""


def _llm_case_text(
    db: Session,
    row: AssessmentSessionRow,
    case_skills: list[SessionCompetencySkill],
    *,
    ordinal: int,
    total: int,
) -> tuple[str | None, dict[str, int] | None, str | None]:
    primary = case_skills[0]
    primary_title = _clean_skill_label_for_telegram(primary.skill_title) or primary.skill_title
    covered = [_clean_skill_label_for_telegram(s.skill_title) or s.skill_title for s in case_skills]
    focus_topics = _focus_topics_for_session(db, row)
    position = _position_label(row, db)
    regulation = _regulation_snippet(db, row)
    prompt = (
        "Сгенерируй один реалистичный кейс для оценки сотрудника на русском языке.\n"
        "Сделай кейс практическим, деловым и привязанным к роли.\n"
        "Один кейс должен позволять оценить сразу несколько навыков одним цельным ответом.\n\n"
        f"Номер кейса: {ordinal} из {total}\n"
        f"Должность: {position}\n"
        f"Основной навык: {primary_title}\n"
        "Какие навыки должен покрывать кейс:\n- "
        + "\n- ".join(covered)
        + "\n"
        "Темы, которые стоит отразить в ситуации (если уместно, вплети в текст, без отдельного списка инструкций):\n- "
        + "\n- ".join(focus_topics)
        + "\n\n"
        + (f"Выдержка из регламента/референса:\n{regulation}\n\n" if regulation else "")
        + "Формат ответа:\n"
        "Только текст кейса, без заголовков, без списков и без служебных пометок.\n"
        "Один или два абзаца: конкретная рабочая ситуация, конфликт/задача, сроки и риски. "
        "В конце задай прямой вопрос: «Каковы ваши действия?» или близкий по смыслу.\n\n"
        "Критично: не называй навыки, компетенции, матрицу навыков, KPI-названия, skill codes и не объясняй, "
        "что именно проверяется. Не пиши фразы вида «основной навык», «связанные навыки», "
        "«нужно показать навык», «в фокусе компетенция».\n"
        "Не используй таблицы, не упоминай ИИ, не пиши вводные пояснения."
    )
    model = _case_model_name()
    payload, text = _openai_text_response(
        model=model,
        prompt=prompt,
        max_output_tokens=900,
        log_label="case_generation",
    )
    if text:
        return text, llm_costs_svc.usage_from_openai_payload(payload), model
    return None, llm_costs_svc.usage_from_openai_payload(payload), model if payload else None


def _template_case_text(
    db: Session,
    row: AssessmentSessionRow,
    case_skills: list[SessionCompetencySkill],
    *,
    ordinal: int,
    total: int,
) -> str:
    position = _position_label(row, db)
    topics = _focus_topics_for_session(db, row)
    soft_hint = topics[0] if topics else "внутренние регламенты и правила компании"
    variants = [
        (
            f"Возникла рабочая ситуация: клиент просит изменить условия сделки и увеличить скидку, "
            f"при этом грозит уйти к конкуренту. Решение нужно принять до конца рабочего дня, "
            f"не нарушая {soft_hint}. Ошибка повлияет на результат подразделения. Каковы ваши действия?"
        ),
        (
            f"Возникла рабочая ситуация: после сложного разговора клиент сомневается, продолжать ли работу с компанией, "
            f"и просит немедленно пересмотреть предложение. У вас ограниченное время, нужно учесть "
            f"{soft_hint} и не допустить потери клиента. Каковы ваши действия?"
        ),
        (
            f"Возникла рабочая ситуация: в роли {position} вы получили срочный запрос, который нельзя закрыть "
            f"по стандартному сценарию без риска для сроков, качества или финансового результата. "
            f"Нужно быстро принять решение, согласовать шаги и соблюсти {soft_hint}. Каковы ваши действия?"
        ),
    ]
    return variants[(ordinal - 1) % len(variants)]


def _generate_case_item(
    db: Session,
    row: AssessmentSessionRow,
    case_skills: list[SessionCompetencySkill],
    *,
    ordinal: int,
    total: int,
    costs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    primary = case_skills[0]
    text, usage, model = _llm_case_text(db, row, case_skills, ordinal=ordinal, total=total)
    if costs is not None and model:
        llm_costs_svc.add_step_cost(costs, step=_STEP_CASE_GENERATION, model=model, usage=usage)
    source = _CASE_SOURCE_LLM if text else _CASE_SOURCE_TEMPLATE
    if not text:
        text = _template_case_text(db, row, case_skills, ordinal=ordinal, total=total)
    return {
        "case_id": f"case_{ordinal}_{uuid.uuid4().hex[:8]}",
        "skill_id": primary.public_skill_id,
        "skill_code": primary.skill_code,
        "skill_title": primary.skill_title,
        "covered_skills": [_skill_ref(skill) for skill in case_skills],
        "text": text,
        "source": source,
        "answer": "",
        "passed": None,
        "case_level_0_3": None,
        "case_pct_0_100": None,
        "evaluation_note": None,
        "skill_evaluations": [],
    }


def _normalize_case_entry(case: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    changed = False
    out = dict(case)
    covered = out.get("covered_skills")
    if not isinstance(covered, list) or not covered:
        out["covered_skills"] = [_skill_ref_from_case(out)]
        changed = True
    else:
        norm_covered = []
        for item in covered:
            if not isinstance(item, dict):
                continue
            ref = {
                "skill_id": str(item.get("skill_id") or ""),
                "skill_code": str(item.get("skill_code") or ""),
                "skill_title": str(item.get("skill_title") or ""),
            }
            if ref["skill_id"]:
                norm_covered.append(ref)
        if not norm_covered:
            norm_covered = [_skill_ref_from_case(out)]
            changed = True
        if norm_covered != covered:
            out["covered_skills"] = norm_covered
            changed = True
    if "case_level_0_3" not in out:
        out["case_level_0_3"] = 2 if out.get("passed") is True else (0 if out.get("passed") is False else None)
        changed = True
    if "case_pct_0_100" not in out:
        out["case_pct_0_100"] = 100 if out.get("passed") is True else (0 if out.get("passed") is False else None)
        changed = True
    raw_skill_evals = out.get("skill_evaluations")
    if not isinstance(raw_skill_evals, list):
        out["skill_evaluations"] = []
        changed = True
    else:
        norm_skill_evals = []
        for item in raw_skill_evals:
            norm_item = _normalize_skill_evaluation(item)
            if norm_item is not None:
                norm_skill_evals.append(norm_item)
        if norm_skill_evals != raw_skill_evals:
            out["skill_evaluations"] = norm_skill_evals
            changed = True
    return out, changed


def _normalize_cases_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        return payload, False
    changed = payload.get("version") != _PART2_CASES_VERSION
    norm_cases = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        norm_case, case_changed = _normalize_case_entry(case)
        norm_cases.append(norm_case)
        changed = changed or case_changed
    out = dict(payload)
    out["version"] = _PART2_CASES_VERSION
    out["cases"] = norm_cases
    out["case_count"] = int(out.get("case_count") or len(norm_cases) or 0)
    out["allotted_minutes"] = int(out.get("allotted_minutes") or 0)
    consensus = _normalize_ai_commission_consensus(out.get("ai_commission_consensus"), payload=out)
    if consensus != out.get("ai_commission_consensus"):
        out["ai_commission_consensus"] = consensus
        changed = True
    costs = _normalize_llm_costs(out.get("llm_costs"))
    if costs != out.get("llm_costs"):
        out["llm_costs"] = costs
        changed = True
    return out, changed


def _covered_skill_ids_from_cases(cases: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for case in cases:
        covered = case.get("covered_skills") if isinstance(case.get("covered_skills"), list) else []
        for item in covered:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("skill_id") or "")
            if skill_id and skill_id not in seen:
                seen.add(skill_id)
                ids.append(skill_id)
    return ids


def _remaining_skills(db: Session, row: AssessmentSessionRow, cases: list[dict[str, Any]]) -> list[SessionCompetencySkill]:
    covered_ids = set(_covered_skill_ids_from_cases(cases))
    return [skill for skill in _all_skills(db, row) if skill.public_skill_id not in covered_ids]


def _payload_covered_skills(cases: list[dict[str, Any]]) -> list[Part2SkillRefOut]:
    seen: set[str] = set()
    refs: list[Part2SkillRefOut] = []
    for case in cases:
        covered = case.get("covered_skills") if isinstance(case.get("covered_skills"), list) else []
        for item in covered:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("skill_id") or "")
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            refs.append(
                Part2SkillRefOut(
                    skill_id=skill_id,
                    skill_code=str(item.get("skill_code") or ""),
                    skill_title=str(item.get("skill_title") or ""),
                )
            )
    return refs


def _remaining_skills_for_payload(
    db: Session, row: AssessmentSessionRow, cases: list[dict[str, Any]]
) -> list[Part2SkillRefOut]:
    return [Part2SkillRefOut(**_skill_ref(skill)) for skill in _remaining_skills(db, row, cases)]


def _build_case_items(
    db: Session,
    row: AssessmentSessionRow,
    skills: list[SessionCompetencySkill],
    *,
    preferred_case_count: int | None = None,
    start_ordinal: int = 1,
    costs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    groups = _group_skills_for_cases(
        skills,
        _recommended_case_count(len(skills), preferred_case_count),
    )
    total = start_ordinal - 1 + len(groups)
    return [
        _generate_case_item(db, row, group, ordinal=start_ordinal + idx, total=total, costs=costs)
        for idx, group in enumerate(groups)
    ]


def _ensure_cases_payload(db: Session, row: AssessmentSessionRow) -> dict[str, Any]:
    payload = _parse_cases_payload(getattr(row, "part2_cases_json", None))
    cases = payload.get("cases")
    if isinstance(cases, list) and cases:
        norm_payload, changed = _normalize_cases_payload(payload)
        if changed:
            row.part2_cases_json = _dump_cases_payload(norm_payload)
            db.commit()
            db.refresh(row)
        return norm_payload

    skills = _all_skills(db, row)
    if not skills:
        raise HTTPException(status_code=404, detail="skill_not_found")
    costs = _default_llm_costs()
    case_items = _build_case_items(
        db,
        row,
        skills,
        preferred_case_count=_requested_case_count(db, row, len(skills)),
        costs=costs,
    )
    total = len(case_items)
    minutes = total * _case_minutes_per_item()
    payload = {
        "version": _PART2_CASES_VERSION,
        "case_count": total,
        "allotted_minutes": minutes,
        "completed": False,
        "completed_at": None,
        "solved_cases": 0,
        "overall_pct": 0,
        "ai_commission_consensus": None,
        "llm_costs": costs,
        "cases": case_items,
    }
    row.part2_cases_json = _dump_cases_payload(payload)
    db.commit()
    db.refresh(row)
    return payload


def _case_item_out(case: dict[str, Any]) -> Part2CaseItemOut:
    return Part2CaseItemOut(
        case_id=str(case.get("case_id") or ""),
        skill_id=str(case.get("skill_id") or ""),
        skill_code=str(case.get("skill_code") or ""),
        skill_title=str(case.get("skill_title") or ""),
        covered_skills=[
            Part2SkillRefOut(
                skill_id=str(s.get("skill_id") or ""),
                skill_code=str(s.get("skill_code") or ""),
                skill_title=str(s.get("skill_title") or ""),
            )
            for s in (case.get("covered_skills") if isinstance(case.get("covered_skills"), list) else [])
            if isinstance(s, dict)
        ],
        text=str(case.get("text") or ""),
        source=str(case.get("source") or _CASE_SOURCE_TEMPLATE),
        answer=str(case.get("answer") or ""),
        passed=case.get("passed") if isinstance(case.get("passed"), bool) or case.get("passed") is None else None,
        case_level_0_3=(
            max(0, min(3, int(case.get("case_level_0_3"))))
            if case.get("case_level_0_3") is not None
            else None
        ),
        case_pct_0_100=(
            max(0, min(100, int(case.get("case_pct_0_100"))))
            if case.get("case_pct_0_100") is not None
            else None
        ),
        evaluation_note=(str(case.get("evaluation_note")) if case.get("evaluation_note") is not None else None),
        skill_evaluations=[
            Part2SkillEvaluationOut(**item)
            for item in (
                _normalize_skill_evaluation(x)
                for x in (case.get("skill_evaluations") if isinstance(case.get("skill_evaluations"), list) else [])
            )
            if item is not None
        ],
    )


def _llm_costs_out(costs: Any) -> Part2LlmCostsOut:
    normalized = _normalize_llm_costs(costs)
    return Part2LlmCostsOut(
        currency=str(normalized.get("currency") or "USD/RUB"),
        usd_to_rub_rate=float(normalized.get("usd_to_rub_rate") or 0.0),
        steps=[
            Part2LlmCostStepOut(
                step=str(item.get("step") or ""),
                label=str(item.get("label") or ""),
                model=(str(item.get("model")) if item.get("model") is not None else None),
                calls=int(item.get("calls") or 0),
                usage_missing_calls=int(item.get("usage_missing_calls") or 0),
                input_tokens=int(item.get("input_tokens") or 0),
                output_tokens=int(item.get("output_tokens") or 0),
                total_tokens=int(item.get("total_tokens") or 0),
                cost_usd=float(item.get("cost_usd") or 0.0),
                cost_rub=float(item.get("cost_rub") or 0.0),
            )
            for item in normalized.get("steps", [])
            if isinstance(item, dict)
        ],
        total_cost_usd=float(normalized.get("total_cost_usd") or 0.0),
        total_cost_rub=float(normalized.get("total_cost_rub") or 0.0),
        total_input_tokens=int(normalized.get("total_input_tokens") or 0),
        total_output_tokens=int(normalized.get("total_output_tokens") or 0),
        total_tokens=int(normalized.get("total_tokens") or 0),
    )


def _ai_commission_out(payload: dict[str, Any]) -> Part2AiCommissionConsensusOut | None:
    normalized = _normalize_ai_commission_consensus(payload.get("ai_commission_consensus"), payload=payload)
    if normalized is None:
        return None
    return Part2AiCommissionConsensusOut(**normalized)


def _part2_cases_public_out(db: Session, row: AssessmentSessionRow, payload: dict[str, Any]) -> Part2CasesPublicOut:
    raw_cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    remaining = _remaining_skills_for_payload(db, row, raw_cases)
    items = [_case_item_out(c) for c in raw_cases]
    ph = getattr(row, "phase", None) or SessionPhase.DRAFT.value
    return Part2CasesPublicOut(
        session_id=row.id,
        phase=SessionPhase(ph),
        case_count=int(payload.get("case_count") or len(items) or 0),
        allotted_minutes=int(payload.get("allotted_minutes") or 0),
        completed=bool(payload.get("completed")),
        completed_at=payload.get("completed_at"),
        solved_cases=int(payload.get("solved_cases") or 0),
        overall_pct=int(payload.get("overall_pct") or 0),
        covered_skills=_payload_covered_skills(raw_cases),
        remaining_skills=remaining,
        can_offer_additional_cases=bool(remaining) and not bool(payload.get("completed")),
        ai_commission_consensus=_ai_commission_out(payload),
        cases=items,
    )


def _part2_cases_hr_out(db: Session, row: AssessmentSessionRow, payload: dict[str, Any]) -> Part2CasesHrOut:
    public = _part2_cases_public_out(db, row, payload)
    return Part2CasesHrOut(**public.model_dump(), llm_costs=_llm_costs_out(payload.get("llm_costs")))


def _overall_case_pct(case_count: int, solved_cases: int) -> int:
    cc = max(1, int(case_count))
    sc = max(0, min(cc, int(solved_cases)))
    if cc in _PASS_PCT_MAP:
        return int(_PASS_PCT_MAP.get(cc, {}).get(sc, 0))
    return int(round((100.0 * sc) / cc))


def _heuristic_case_evaluation(case_text: str, answer: str) -> dict[str, Any]:
    txt = " ".join((answer or "").split())
    words = [w for w in txt.split(" ") if w]
    if len(words) >= 15:
        return {
            "passed": True,
            "case_level_0_3": 2,
            "case_pct_0_100": 75,
            "note": "Ответ достаточно развёрнутый: есть шанс считать кейс решённым на базовом уровне.",
        }
    return {
        "passed": False,
        "case_level_0_3": 0,
        "case_pct_0_100": 20,
        "note": "Ответ слишком короткий: не хватает шагов, аргументов и привязки к регламенту/рискам.",
    }


def _llm_case_evaluation(case_text: str, answer: str) -> tuple[dict[str, Any] | None, dict[str, int] | None, str | None]:
    prompt = (
        "Ты оцениваешь решение кейса сотрудником.\n"
        "Верни строго JSON-объект без markdown:\n"
        '{"passed": true|false, "case_level_0_3": 0..3, "case_pct_0_100": 0..100, "note": "краткое объяснение до 240 символов"}\n\n'
        "Кейс:\n"
        f"{case_text}\n\n"
        "Ответ сотрудника:\n"
        f"{answer}\n"
    )
    model = _case_eval_model_name()
    payload, text = _openai_text_response(
        model=model,
        prompt=prompt,
        max_output_tokens=260,
        log_label="case_evaluation",
    )
    parsed = _extract_json_object(text)
    if not parsed:
        return None, llm_costs_svc.usage_from_openai_payload(payload), model if payload else None
    passed = bool(parsed.get("passed"))
    level = parsed.get("case_level_0_3")
    pct = parsed.get("case_pct_0_100")
    level_int = max(0, min(3, int(level))) if level is not None else None
    pct_int = max(0, min(100, int(pct))) if pct is not None else None
    if level_int is None and pct_int is not None:
        level_int = _level_from_pct(pct_int)
    if pct_int is None and level_int is not None:
        pct_int = _pct_for_level(level_int)
    note = str(parsed.get("note") or "").strip()
    return (
        {
            "passed": passed,
            "case_level_0_3": level_int,
            "case_pct_0_100": pct_int,
            "note": note[:240] if note else ("Кейс решён." if passed else "Кейс не решён."),
        },
        llm_costs_svc.usage_from_openai_payload(payload),
        model,
    )


def _heuristic_skill_evaluations(case: dict[str, Any], answer: str, case_eval: dict[str, Any]) -> list[dict[str, Any]]:
    covered = case.get("covered_skills") if isinstance(case.get("covered_skills"), list) else []
    words = [w for w in " ".join((answer or "").split()).split(" ") if w]
    base_level = int(case_eval.get("case_level_0_3") or 0)
    if len(words) >= 35:
        base_level = max(base_level, 3)
    elif len(words) >= 15:
        base_level = max(base_level, 2)
    elif len(words) >= 8:
        base_level = max(base_level, 1)
    pct = _pct_for_level(base_level)
    evidence = " ".join((answer or "").split())[:220] if answer else None
    gaps = None if base_level >= 2 else "Нужно больше конкретики: шаги, риски, регламенты и критерии результата."
    out: list[dict[str, Any]] = []
    for item in covered:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "skill_id": str(item.get("skill_id") or ""),
                "skill_code": str(item.get("skill_code") or ""),
                "skill_title": str(item.get("skill_title") or ""),
                "level_0_3": base_level,
                "pct_0_100": pct,
                "evidence": evidence,
                "gaps": gaps,
            }
        )
    return [item for item in out if item.get("skill_id")]


def _llm_skill_evaluations(
    case: dict[str, Any],
    answer: str,
) -> tuple[list[dict[str, Any]] | None, dict[str, int] | None, str | None]:
    covered = case.get("covered_skills") if isinstance(case.get("covered_skills"), list) else []
    if not covered:
        return [], None, None
    skills_block = "\n".join(
        f'- {str(item.get("skill_title") or "")} ({str(item.get("skill_code") or "")}), skill_id={str(item.get("skill_id") or "")}'
        for item in covered
        if isinstance(item, dict)
    )
    prompt = (
        "Ты оцениваешь покрытие навыков в ответе сотрудника по кейсу.\n"
        "Верни строго JSON-объект без markdown:\n"
        '{"skills":[{"skill_id":"...", "level_0_3":0..3, "pct_0_100":0..100, "evidence":"краткое подтверждение", "gaps":"краткий пробел"}]}\n\n'
        "Навыки для оценки:\n"
        f"{skills_block}\n\n"
        "Кейс:\n"
        f"{str(case.get('text') or '')}\n\n"
        "Ответ сотрудника:\n"
        f"{answer}\n"
    )
    model = _case_eval_model_name()
    payload, text = _openai_text_response(
        model=model,
        prompt=prompt,
        max_output_tokens=600,
        log_label="skill_evaluation",
    )
    parsed = _extract_json_object(text)
    if not parsed:
        return None, llm_costs_svc.usage_from_openai_payload(payload), model if payload else None
    raw_items = parsed.get("skills")
    if not isinstance(raw_items, list):
        return None, llm_costs_svc.usage_from_openai_payload(payload), model if payload else None
    covered_by_id = {
        str(item.get("skill_id") or ""): {
            "skill_code": str(item.get("skill_code") or ""),
            "skill_title": str(item.get("skill_title") or ""),
        }
        for item in covered
        if isinstance(item, dict)
    }
    out: list[dict[str, Any]] = []
    for item in raw_items:
        norm = _normalize_skill_evaluation(item)
        if norm is None:
            continue
        src = covered_by_id.get(norm["skill_id"]) or {}
        if not norm.get("skill_code"):
            norm["skill_code"] = str(src.get("skill_code") or "")
        if not norm.get("skill_title"):
            norm["skill_title"] = str(src.get("skill_title") or "")
        out.append(norm)
    return out, llm_costs_svc.usage_from_openai_payload(payload), model


def _skill_evaluations_for_case(
    case: dict[str, Any],
    answer: str,
    case_eval: dict[str, Any],
    *,
    costs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    llm_items, usage, model = _llm_skill_evaluations(case, answer)
    if costs is not None and model:
        llm_costs_svc.add_step_cost(costs, step=_STEP_SKILL_EVALUATION, model=model, usage=usage)
    if llm_items is not None:
        return llm_items
    return _heuristic_skill_evaluations(case, answer, case_eval)


def _heuristic_ai_commission(payload: dict[str, Any]) -> dict[str, Any] | None:
    raw_cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    all_skill_evals = [
        item
        for case in raw_cases
        for item in (case.get("skill_evaluations") if isinstance(case.get("skill_evaluations"), list) else [])
        if isinstance(item, dict) and str(item.get("skill_id") or "").strip()
    ]
    if not raw_cases and not all_skill_evals:
        return None
    if all_skill_evals:
        avg_pct = round(
            sum(int(item.get("pct_0_100") or 0) for item in all_skill_evals) / max(1, len(all_skill_evals))
        )
    else:
        avg_pct = int(payload.get("overall_pct") or 0)
    strengths = []
    risks = []
    for item in all_skill_evals:
        title = str(item.get("skill_title") or item.get("skill_code") or item.get("skill_id") or "").strip()
        pct = int(item.get("pct_0_100") or 0)
        if pct >= 70 and title:
            strengths.append(title)
        elif pct < 50 and title:
            risks.append(title)
    return {
        "overall_level_0_3": _level_from_pct(avg_pct),
        "overall_pct_0_100": avg_pct,
        "summary": f"AI-комиссия оценивает блок кейсов на {avg_pct}% по совокупности ответов и покрытых навыков.",
        "recommendation": (
            "Рекомендуется подтвердить сильные стороны на этапе оценки руководителем."
            if avg_pct >= 65
            else "Нужен разбор пробелов и дополнительная верификация руководителем."
        ),
        "strengths": list(dict.fromkeys(strengths))[:5],
        "risks": list(dict.fromkeys(risks))[:5],
    }


def _llm_ai_commission(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, int] | None, str | None]:
    raw_cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    if not raw_cases:
        return None, None, None
    case_lines = []
    for idx, case in enumerate(raw_cases, start=1):
        skill_lines = []
        for item in (case.get("skill_evaluations") if isinstance(case.get("skill_evaluations"), list) else []):
            if not isinstance(item, dict):
                continue
            skill_lines.append(
                f"- {str(item.get('skill_title') or item.get('skill_code') or item.get('skill_id') or '')}: "
                f"level={item.get('level_0_3')}, pct={item.get('pct_0_100')}, "
                f"evidence={str(item.get('evidence') or '')}, gaps={str(item.get('gaps') or '')}"
            )
        case_lines.append(
            f"Кейс {idx}: passed={case.get('passed')}, case_level={case.get('case_level_0_3')}, "
            f"case_pct={case.get('case_pct_0_100')}, note={str(case.get('evaluation_note') or '')}\n"
            f"Навыки:\n" + ("\n".join(skill_lines) if skill_lines else "- нет skill-level данных")
        )
    prompt = (
        "Ты — итоговая AI-комиссия по блоку кейсов сотрудника.\n"
        "Верни строго JSON-объект без markdown:\n"
        '{"overall_level_0_3":0..3, "overall_pct_0_100":0..100, "summary":"1-2 предложения", '
        '"recommendation":"1 предложение", "strengths":["..."], "risks":["..."]}\n\n'
        "Материалы по кейсам:\n"
        + "\n\n".join(case_lines)
    )
    model = _case_eval_model_name()
    payload_raw, text = _openai_text_response(
        model=model,
        prompt=prompt,
        max_output_tokens=420,
        log_label="ai_commission",
    )
    parsed = _extract_json_object(text)
    if not parsed:
        return None, llm_costs_svc.usage_from_openai_payload(payload_raw), model if payload_raw else None
    norm = _normalize_ai_commission_consensus(parsed, payload=payload)
    return norm, llm_costs_svc.usage_from_openai_payload(payload_raw), model


def _ai_commission_for_payload(payload: dict[str, Any], *, costs: dict[str, Any] | None = None) -> dict[str, Any] | None:
    llm_value, usage, model = _llm_ai_commission(payload)
    if costs is not None and model:
        llm_costs_svc.add_step_cost(costs, step=_STEP_AI_COMMISSION, model=model, usage=usage)
    if llm_value is not None:
        return llm_value
    return _heuristic_ai_commission(payload)


def _evaluate_case(
    case_text: str,
    answer: str,
    *,
    costs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    llm, usage, model = _llm_case_evaluation(case_text, answer)
    if costs is not None and model:
        llm_costs_svc.add_step_cost(costs, step=_STEP_CASE_EVALUATION, model=model, usage=usage)
    if llm is not None:
        return llm
    return _heuristic_case_evaluation(case_text, answer)


def _aggregate_skill_evaluations(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_skill: dict[str, dict[str, Any]] = {}
    for case in cases:
        raw_items = case.get("skill_evaluations") if isinstance(case.get("skill_evaluations"), list) else []
        for raw in raw_items:
            item = _normalize_skill_evaluation(raw)
            if item is None:
                continue
            skill_id = item["skill_id"]
            current = by_skill.get(skill_id)
            current_pct = int(current.get("pct_0_100") or -1) if current else -1
            item_pct = int(item.get("pct_0_100") or 0)
            if current is None or item_pct >= current_pct:
                by_skill[skill_id] = dict(item)
    return list(by_skill.values())


def _upsert_case_skill_results(db: Session, row: AssessmentSessionRow, cases: list[dict[str, Any]]) -> None:
    aggregated = _aggregate_skill_evaluations(cases)
    if not aggregated:
        return
    skill_map = session_competency_skill_map(db, row, include_inactive=False, ensure_result_skills=True)
    existing_rows = list(
        db.scalars(select(SkillAssessmentResultRow).where(SkillAssessmentResultRow.session_id == row.id)).all()
    )
    case_rows_by_skill: dict[str, SkillAssessmentResultRow] = {}
    for result_row in existing_rows:
        notes = assessment_svc._evidence_from_json(result_row.evidence_json)
        if notes.get(EvidenceKind.CASE):
            case_rows_by_skill[result_row.skill_id] = result_row
    for item in aggregated:
        public_skill_id = str(item.get("skill_id") or "")
        if not public_skill_id:
            continue
        skill_ref = skill_map.get(public_skill_id)
        result_skill_id = skill_ref.result_skill_id if skill_ref is not None else None
        if not result_skill_id:
            continue
        evidence_parts = []
        if item.get("evidence"):
            evidence_parts.append(str(item.get("evidence")))
        if item.get("gaps"):
            evidence_parts.append("Пробелы: " + str(item.get("gaps")))
        note = "\n".join(evidence_parts)[:2000] if evidence_parts else "Оценка Part 2"
        level = int(item.get("level_0_3") or _level_from_pct(item.get("pct_0_100")) or 0)
        result_row = case_rows_by_skill.get(result_skill_id)
        if result_row is None:
            result_row = SkillAssessmentResultRow(
                id=str(uuid.uuid4()),
                session_id=row.id,
                skill_id=result_skill_id,
                level=level,
                evidence_json=assessment_svc._evidence_to_json({EvidenceKind.CASE: note}),
            )
            db.add(result_row)
            case_rows_by_skill[result_skill_id] = result_row
            continue
        notes = assessment_svc._evidence_from_json(result_row.evidence_json)
        notes[EvidenceKind.CASE] = note
        result_row.level = level
        result_row.evidence_json = assessment_svc._evidence_to_json(notes)


def get_session_cases(db: Session, session_id: str) -> Part2CasesHrOut:
    row = _resolve_session_row(db, session_id)
    payload = _ensure_cases_payload(db, row)
    return _part2_cases_hr_out(db, row, payload)


def get_public_cases(db: Session, token: str) -> Part2CasesPublicOut:
    row = _resolve_public_session_row(db, token)
    payload = _ensure_cases_payload(db, row)
    return _part2_cases_public_out(db, row, payload)


def submit_session_cases(db: Session, session_id: str, body: Part2CasesSubmit) -> Part2CasesHrOut:
    row = _resolve_session_row(db, session_id)
    payload = _ensure_cases_payload(db, row)
    cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    if not cases:
        raise HTTPException(status_code=400, detail="part2_cases_not_generated")
    answers_by_id = {str(x.case_id): x.answer.strip() for x in body.answers if x.answer and x.answer.strip()}
    missing = [str(c.get("case_id") or "") for c in cases if str(c.get("case_id") or "") not in answers_by_id]
    if missing:
        raise HTTPException(status_code=400, detail="part2_cases_incomplete:" + ",".join(missing))

    solved = 0
    costs = payload.setdefault("llm_costs", _default_llm_costs())
    for case in cases:
        case_id = str(case.get("case_id") or "")
        answer = answers_by_id[case_id]
        case_eval = _evaluate_case(str(case.get("text") or ""), answer, costs=costs)
        skill_evaluations = _skill_evaluations_for_case(case, answer, case_eval, costs=costs)
        case["answer"] = answer
        case["passed"] = bool(case_eval.get("passed"))
        case["case_level_0_3"] = case_eval.get("case_level_0_3")
        case["case_pct_0_100"] = case_eval.get("case_pct_0_100")
        case["evaluation_note"] = case_eval.get("note")
        case["skill_evaluations"] = skill_evaluations
        if case["passed"]:
            solved += 1

    payload["cases"] = cases
    payload["completed"] = True
    payload["completed_at"] = datetime.now(timezone.utc).isoformat()
    payload["solved_cases"] = solved
    payload["overall_pct"] = _overall_case_pct(len(cases), solved)
    payload["ai_commission_consensus"] = _ai_commission_for_payload(payload, costs=costs)
    _upsert_case_skill_results(db, row, cases)
    row.part2_cases_json = _dump_cases_payload(payload)
    row.phase = SessionPhase.PART3.value
    db.commit()
    db.refresh(row)
    try:
        send_part2_protocol_ready_notice(db, row.id)
    except Exception:
        _log.exception("part2_case: failed to notify employee protocol for session %s", row.id[:8])
    try:
        manager_assessment_svc.send_manager_assessment_ready_notice(db, row.id)
    except Exception:
        _log.exception("part2_case: failed to notify manager for session %s", row.id[:8])
    return _part2_cases_hr_out(db, row, payload)


def submit_public_cases(db: Session, token: str, body: Part2CasesSubmit) -> Part2CasesPublicOut:
    row = _resolve_public_session_row(db, token)
    submit_session_cases(db, row.id, body)
    db.refresh(row)
    payload = _ensure_cases_payload(db, row)
    return _part2_cases_public_out(db, row, payload)


def offer_additional_session_cases(
    db: Session,
    session_id: str,
    body: Part2AdditionalCasesRequest | None = None,
) -> Part2CasesHrOut:
    row = _resolve_session_row(db, session_id)
    payload = _ensure_cases_payload(db, row)
    if bool(payload.get("completed")):
        raise HTTPException(status_code=400, detail="part2_cases_already_completed")

    existing_cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    remaining_by_id = {skill.public_skill_id: skill for skill in _remaining_skills(db, row, existing_cases)}
    requested_ids = []
    if body is not None and body.skill_ids:
        for raw in body.skill_ids:
            skill_id = str(raw or "").strip()
            if not skill_id:
                continue
            if skill_id not in requested_ids:
                requested_ids.append(skill_id)
    if requested_ids:
        invalid = [skill_id for skill_id in requested_ids if skill_id not in remaining_by_id]
        if invalid:
            raise HTTPException(status_code=400, detail="part2_additional_skills_invalid:" + ",".join(invalid))
        selected_skills = [remaining_by_id[skill_id] for skill_id in requested_ids]
    else:
        selected_skills = list(remaining_by_id.values())
    if not selected_skills:
        raise HTTPException(status_code=400, detail="part2_no_remaining_skills_for_additional_cases")

    extra_cases = _build_case_items(
        db,
        row,
        selected_skills,
        preferred_case_count=_recommended_case_count(len(selected_skills)),
        start_ordinal=len(existing_cases) + 1,
        costs=payload.setdefault("llm_costs", _default_llm_costs()),
    )
    payload["cases"] = existing_cases + extra_cases
    payload["case_count"] = len(payload["cases"])
    payload["allotted_minutes"] = int(payload["case_count"]) * _case_minutes_per_item()
    row.part2_cases_json = _dump_cases_payload(payload)
    db.commit()
    db.refresh(row)
    return _part2_cases_hr_out(db, row, payload)


def get_session_case(db: Session, session_id: str, skill_id: str | None = None) -> CaseTextOut:
    row = _resolve_session_row(db, session_id)
    payload = _ensure_cases_payload(db, row)
    cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    if not cases:
        raise HTTPException(status_code=404, detail="part2_cases_not_found")
    chosen = None
    if skill_id and str(skill_id).strip():
        wanted = str(skill_id).strip()
        chosen = next(
            (
                c
                for c in cases
                if str(c.get("skill_id") or "") == wanted
                or wanted in {
                    str(s.get("skill_id") or "")
                    for s in (c.get("covered_skills") if isinstance(c.get("covered_skills"), list) else [])
                    if isinstance(s, dict)
                }
            ),
            None,
        )
    if chosen is None:
        chosen = cases[0]
    return CaseTextOut(
        session_id=row.id,
        skill_id=str(chosen.get("skill_id") or ""),
        skill_code=str(chosen.get("skill_code") or ""),
        skill_title=str(chosen.get("skill_title") or ""),
        text=str(chosen.get("text") or ""),
        source=str(chosen.get("source") or _CASE_SOURCE_TEMPLATE),
    )


def get_public_case(db: Session, token: str, skill_id: str | None = None) -> CaseTextOut:
    row = _resolve_public_session_row(db, token)
    return get_session_case(db, row.id, skill_id)


def get_part2_summary(row: AssessmentSessionRow) -> str:
    payload = _parse_cases_payload(getattr(row, "part2_cases_json", None))
    if not payload:
        return "кейсы ещё не сформированы"
    case_count = int(payload.get("case_count") or 0)
    minutes = int(payload.get("allotted_minutes") or 0)
    if bool(payload.get("completed")):
        solved = int(payload.get("solved_cases") or 0)
        pct = int(payload.get("overall_pct") or 0)
        return f"кейсы: {solved}/{case_count} = {pct}%"
    return f"кейсы назначены: {case_count}, время на решение: {minutes} мин."


def send_part2_protocol_ready_notice(db: Session, session_id: str) -> dict[str, Any]:
    row = _resolve_session_row(db, session_id)
    chat_id = _resolve_case_chat_id(db, row)
    token = part1_docs_svc.ensure_part1_docs_access_token(db, row)
    if not chat_id:
        return {"sent": False, "reason": "no_chat_id"}
    use_mock = (os.getenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND") or "").strip().lower() == "mock"
    token_env = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not use_mock and (not token_env or len(token_env) < 10):
        return {"sent": False, "reason": "no_bot_token"}

    bundle = get_session_cases(db, session_id)
    report_url = build_public_report_absolute_url(db, session_id)
    report_path = build_public_report_path(token)
    emp = get_employee(db, row.client_id, row.employee_id)
    name = employee_greeting_label(emp) or "коллега"
    text = "\n".join(
        [
            f"Здравствуйте, {name}!",
            "",
            "Оценка по кейсам завершена и добавлена в общий протокол (часть 2).",
            f"Итог ИИ: {bundle.solved_cases}/{bundle.case_count} = {bundle.overall_pct}%.",
            "",
            "Открыть общий протокол:",
            report_url if report_url else report_path,
            "",
            "Этап 3: оценка руководителя уже запущен. Руководителю отправлена ссылка на оценку навыков.",
            "После того как руководитель завершит оценку, вы получите ссылку на полный протокол.",
        ]
    )
    outbound = get_telegram_outbound()
    result = outbound.send_message(
        token=token_env if token_env else "mock_token_for_tests",
        chat_id=chat_id,
        text=text,
        reply_markup=None,
    )
    return {"sent": bool(result.ok), "reason": result.description, "http_status": result.http_status, "chat_id": chat_id}


def _resolve_case_chat_id(db: Session, row: AssessmentSessionRow) -> str | None:
    bind = (
        examination_svc.get_telegram_binding_for_employee(db, row.client_id, row.employee_id)
        if row.employee_id
        else None
    )
    if bind is not None and str(bind.telegram_chat_id).strip():
        return str(bind.telegram_chat_id).strip()
    emp = get_employee(db, row.client_id, row.employee_id)
    if emp is not None and emp.telegram_chat_id and str(emp.telegram_chat_id).strip():
        return str(emp.telegram_chat_id).strip()
    # Как в docs_survey_notify: если нет привязки и нет telegram у сотрудника в HR — тот же fallback (в т.ч. дефолт при отсутствии ключа в env).
    raw = (os.getenv("TELEGRAM_DOCS_SURVEY_FALLBACK_CHAT_ID") or "300398364").strip()
    return raw or None


def find_latest_part1_assessment_session(
    db: Session, *, client_id: str, employee_id: str | None
) -> AssessmentSessionRow | None:
    """Последняя сессия оценки навыков в фазе part1 для пары client/employee (по updated_at)."""
    if not employee_id or not str(employee_id).strip():
        return None
    cid = str(client_id).strip()
    eid = str(employee_id).strip()
    return db.scalar(
        select(AssessmentSessionRow)
        .where(
            AssessmentSessionRow.client_id == cid,
            AssessmentSessionRow.employee_id == eid,
            AssessmentSessionRow.phase == SessionPhase.PART1.value,
            AssessmentSessionRow.status != AssessmentSessionStatus.CANCELLED.value,
        )
        .order_by(AssessmentSessionRow.updated_at.desc())
        .limit(1)
    )


def on_examination_completed_advance_skill_assessment(
    db: Session, exam_row: Any, *, preferred_chat_id: str | None = None, send_telegram: bool = True
) -> dict[str, Any]:
    """
    После завершения экзамена по регламентам (Telegram/API): если есть сессия оценки в part1 —
    переводим в part2 и шлём в Telegram ссылку на страницу кейсов (как при закрытии чек-листа Part1).
    """
    sa_row = find_latest_part1_assessment_session(db, client_id=exam_row.client_id, employee_id=exam_row.employee_id)
    if sa_row is None:
        return {"advanced": False, "reason": "assessment_part1_session_not_found"}
    sa_row.phase = SessionPhase.PART2.value
    db.commit()
    db.refresh(sa_row)
    try:
        sent = (
            send_part2_case_ready_notice(db, sa_row.id, preferred_chat_id=preferred_chat_id)
            if send_telegram
            else prepare_part2_case_ready_notice(db, sa_row.id, preferred_chat_id=preferred_chat_id)
        )
        return {"advanced": True, "assessment_session_id": sa_row.id, "part2_notice": sent}
    except Exception as e:
        _log.exception(
            "part2_case: failed to send part2 notice after examination complete for assessment session %s",
            sa_row.id,
        )
        return {
            "advanced": True,
            "assessment_session_id": sa_row.id,
            "part2_notice": {"sent": False, "reason": str(e)},
        }


def send_part2_case_ready_notice(
    db: Session, session_id: str, skill_id: str | None = None, *, preferred_chat_id: str | None = None
) -> dict[str, Any]:
    prepared = prepare_part2_case_ready_notice(db, session_id, skill_id, preferred_chat_id=preferred_chat_id)
    chat_id = prepared.get("chat_id")
    if not chat_id:
        return prepared
    use_mock = (os.getenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND") or "").strip().lower() == "mock"
    token_env = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not use_mock and (not token_env or len(token_env) < 10):
        prepared["sent"] = False
        prepared["reason"] = "no_bot_token"
        return prepared
    message_queue = list(prepared.get("messages") or [])

    send_token = token_env if token_env else "mock_token_for_tests"
    outbound = get_telegram_outbound()
    results: list[Any] = []
    for part in message_queue:
        if not (part or "").strip():
            continue
        results.append(
            outbound.send_message(
                token=send_token,
                chat_id=chat_id,
                text=part.strip(),
                reply_markup=None,
            )
        )

    all_ok = bool(results) and all(getattr(r, "ok", False) for r in results)
    last = results[-1] if results else None
    prepared["sent"] = all_ok
    prepared["reason"] = getattr(last, "description", None) if last else None
    prepared["http_status"] = getattr(last, "http_status", None) if last else None
    prepared["telegram_messages_sent"] = len(results)
    return prepared
