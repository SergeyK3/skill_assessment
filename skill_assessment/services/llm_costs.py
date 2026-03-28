"""Helper for OpenAI usage parsing and cost aggregation."""

from __future__ import annotations

import os
from typing import Any


_STEP_LABELS: dict[str, str] = {
    "case_generation": "Генерация кейса",
    "case_evaluation": "Оценка кейса",
    "skill_evaluation": "Оценка навыков",
    "ai_commission": "AI-комиссия",
}


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return default


def usd_to_rub_rate() -> float:
    return max(0.0, _float_env("SKILL_ASSESSMENT_USD_TO_RUB", 90.0))


def _step_rate(step: str, direction: str) -> float:
    step_key = step.strip().upper()
    default_name = f"SKILL_ASSESSMENT_LLM_{direction}_USD_PER_1M"
    step_name = f"SKILL_ASSESSMENT_{step_key}_{direction}_USD_PER_1M"
    return max(0.0, _float_env(step_name, _float_env(default_name, 0.0)))


def step_label(step: str) -> str:
    return _STEP_LABELS.get(step, step)


def empty_costs() -> dict[str, Any]:
    return {
        "currency": "USD/RUB",
        "usd_to_rub_rate": usd_to_rub_rate(),
        "steps": [],
        "total_cost_usd": 0.0,
        "total_cost_rub": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_tokens": 0,
    }


def usage_from_openai_payload(payload: dict[str, Any] | None) -> dict[str, int] | None:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    total_tokens = usage.get("total_tokens")
    try:
        in_tokens = max(0, int(input_tokens or 0))
        out_tokens = max(0, int(output_tokens or 0))
        total = max(0, int(total_tokens or (in_tokens + out_tokens)))
    except Exception:
        return None
    return {
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "total_tokens": total,
    }


def add_step_cost(
    costs: dict[str, Any] | None,
    *,
    step: str,
    model: str | None,
    usage: dict[str, int] | None,
) -> dict[str, Any]:
    out = costs if isinstance(costs, dict) else empty_costs()
    out["usd_to_rub_rate"] = usd_to_rub_rate()
    steps = out.get("steps")
    if not isinstance(steps, list):
        steps = []
        out["steps"] = steps
    existing = next((item for item in steps if isinstance(item, dict) and item.get("step") == step), None)
    if existing is None:
        existing = {
            "step": step,
            "label": step_label(step),
            "model": model,
            "calls": 0,
            "usage_missing_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "cost_rub": 0.0,
        }
        steps.append(existing)
    existing["calls"] = int(existing.get("calls") or 0) + 1
    if model and not existing.get("model"):
        existing["model"] = model
    if not usage:
        existing["usage_missing_calls"] = int(existing.get("usage_missing_calls") or 0) + 1
        return recompute_totals(out)

    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    total_tokens = max(0, int(usage.get("total_tokens") or (input_tokens + output_tokens)))
    cost_usd = ((input_tokens / 1_000_000.0) * _step_rate(step, "INPUT")) + (
        (output_tokens / 1_000_000.0) * _step_rate(step, "OUTPUT")
    )
    cost_rub = cost_usd * usd_to_rub_rate()
    existing["input_tokens"] = int(existing.get("input_tokens") or 0) + input_tokens
    existing["output_tokens"] = int(existing.get("output_tokens") or 0) + output_tokens
    existing["total_tokens"] = int(existing.get("total_tokens") or 0) + total_tokens
    existing["cost_usd"] = round(float(existing.get("cost_usd") or 0.0) + cost_usd, 6)
    existing["cost_rub"] = round(float(existing.get("cost_rub") or 0.0) + cost_rub, 4)
    return recompute_totals(out)


def recompute_totals(costs: dict[str, Any] | None) -> dict[str, Any]:
    out = costs if isinstance(costs, dict) else empty_costs()
    steps = out.get("steps")
    if not isinstance(steps, list):
        steps = []
        out["steps"] = steps
    out["total_cost_usd"] = round(sum(float(item.get("cost_usd") or 0.0) for item in steps if isinstance(item, dict)), 6)
    out["total_cost_rub"] = round(sum(float(item.get("cost_rub") or 0.0) for item in steps if isinstance(item, dict)), 4)
    out["total_input_tokens"] = sum(int(item.get("input_tokens") or 0) for item in steps if isinstance(item, dict))
    out["total_output_tokens"] = sum(int(item.get("output_tokens") or 0) for item in steps if isinstance(item, dict))
    out["total_tokens"] = sum(int(item.get("total_tokens") or 0) for item in steps if isinstance(item, dict))
    out["usd_to_rub_rate"] = usd_to_rub_rate()
    return out
