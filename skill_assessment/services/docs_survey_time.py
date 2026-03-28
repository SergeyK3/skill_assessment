# route: (service) | file: skill_assessment/services/docs_survey_time.py
"""Локальная зона для слота опроса по документам (Telegram): календарь → UTC в БД."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, time as dt_time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from skill_assessment.env import load_plugin_env

_log = logging.getLogger(__name__)


def reminder_minutes_before() -> int:
    """Сколько минут до слота уходит напоминание Telegram (``DOCS_SURVEY_REMINDER_MINUTES_BEFORE``)."""
    # override=False: переменные из окружения процесса (в т.ч. тесты) не затираются файлом .env
    load_plugin_env(override=False)
    raw = os.getenv("DOCS_SURVEY_REMINDER_MINUTES_BEFORE", "5").strip()
    try:
        return max(1, min(120, int(raw)))
    except ValueError:
        return 5


def survey_zone_name() -> str:
    """IANA, например ``Europe/Moscow``. Env: ``DOCS_SURVEY_LOCAL_TIMEZONE``."""
    raw = os.getenv("DOCS_SURVEY_LOCAL_TIMEZONE", "").strip()
    return raw or "Europe/Moscow"


def survey_zone() -> ZoneInfo:
    """Зона из ``DOCS_SURVEY_LOCAL_TIMEZONE``; при ошибке — запасные варианты (на Windows нужен пакет ``tzdata``)."""
    name = survey_zone_name()
    for candidate in (name, "Europe/Moscow", "UTC"):
        try:
            return ZoneInfo(candidate)
        except Exception:
            continue
    _log.error("docs_survey_time: не удалось загрузить ни одну IANA-зону — установите пакет tzdata (pip install tzdata)")
    return ZoneInfo("UTC")


def survey_slot_today_bounds_utc_naive() -> tuple[datetime, datetime]:
    """Границы календарного «сегодня» в :func:`survey_zone` как наивные UTC (как в БД)."""
    tz = survey_zone()
    now_l = datetime.now(tz)
    day_start = now_l.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    su = day_start.astimezone(timezone.utc).replace(tzinfo=None)
    eu = day_end.astimezone(timezone.utc).replace(tzinfo=None)
    return su, eu


def local_slot_to_utc_naive(d: date, hour: int, minute: int) -> datetime:
    """
    Дата и время, выбранные в календаре как «локальное рабочее время» в :func:`survey_zone`,
    переводятся в UTC и сохраняются в БД без tz (наивное UTC — единый формат для SQLite).
    """
    tz = survey_zone()
    local_dt = datetime.combine(d, dt_time(hour, minute), tzinfo=tz)
    utc = local_dt.astimezone(timezone.utc)
    return utc.replace(tzinfo=None)


def utc_naive_to_aware_utc(dt: datetime | None) -> datetime | None:
    """Для JSON/API: наивное UTC из БД → aware UTC (сериализация с ``Z``)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def utc_naive_to_local_display(dt_utc_naive: datetime | None) -> str | None:
    """Человекочитаемое локальное время в :func:`survey_zone` + имя зоны."""
    if dt_utc_naive is None:
        return None
    au = utc_naive_to_aware_utc(dt_utc_naive)
    if au is None:
        return None
    local = au.astimezone(survey_zone())
    return f"{local.strftime('%d.%m.%Y %H:%M')} ({survey_zone_name()})"


def utc_naive_slot_to_local_date_time_strings(dt_utc_naive: datetime | None) -> tuple[str | None, str | None]:
    """Дата и время слота в :func:`survey_zone` для полей ``date`` / ``time`` (YYYY-MM-DD и HH:MM)."""
    if dt_utc_naive is None:
        return None, None
    au = utc_naive_to_aware_utc(dt_utc_naive)
    if au is None:
        return None, None
    local = au.astimezone(survey_zone())
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def aware_utc_to_local_label(au: datetime) -> str:
    local = au.astimezone(survey_zone())
    return f"{local.strftime('%d.%m.%Y %H:%M')} ({survey_zone_name()})"


def _minutes_until_utc(aware_utc: datetime) -> int:
    """Целые минуты от текущего момента UTC до ``aware_utc`` (может быть < 0)."""
    now = datetime.now(timezone.utc)
    return int((aware_utc - now).total_seconds() // 60)


def docs_survey_hr_labels(
    *,
    docs_survey_scheduled_at: datetime | None,
    docs_survey_reminder_30m_sent_at: datetime | None,
    docs_survey_pd_consent_status: str | None = None,
) -> dict[str, Any]:
    """
    Подсказки HR: слот в локальном времени, «окно» напоминания Telegram (за N мин до слота).

    ``docs_survey_scheduled_at`` в БД — наивное UTC.

    Напоминание «готов / не готов» в Telegram шлётся только при ``docs_survey_pd_consent_status == accepted``
    (см. ``process_docs_survey_30m_reminders_once``); иначе не заполняем «минуты до напоминания», чтобы не вводить HR в заблуждение.
    """
    tz_name = survey_zone_name()
    n = reminder_minutes_before()
    out: dict[str, Any] = {
        "docs_survey_local_timezone": tz_name,
        "docs_survey_reminder_minutes_before": n,
        "docs_survey_slot_local_label": None,
        "docs_survey_reminder_telegram_local_label": None,
        "docs_survey_telegram_schedule_hint": None,
        "docs_survey_minutes_until_reminder": None,
        "docs_survey_minutes_until_slot": None,
    }
    if docs_survey_scheduled_at is None:
        return out

    slot_local = utc_naive_to_local_display(docs_survey_scheduled_at)
    out["docs_survey_slot_local_label"] = slot_local

    sched_utc = utc_naive_to_aware_utc(docs_survey_scheduled_at)
    if sched_utc is not None:
        out["docs_survey_minutes_until_slot"] = _minutes_until_utc(sched_utc)

    pd = (docs_survey_pd_consent_status or "").strip()
    if pd != "accepted":
        if pd == "timed_out":
            out["docs_survey_telegram_schedule_hint"] = (
                "Напоминание в Telegram с кнопками «Да»/«Нет» не отправляется: истекло время ожидания ответа по согласию "
                "на обработку ПДн (timed_out). Планирователь шлёт напоминание только после принятого согласия."
            )
        elif pd == "declined":
            out["docs_survey_telegram_schedule_hint"] = (
                "Напоминание «готов / не готов» не отправляется: сотрудник отказался от согласия на ПДн. "
                "Нужен контакт через HR."
            )
        elif pd == "awaiting_first" or not pd:
            out["docs_survey_telegram_schedule_hint"] = (
                "Напоминание не отправляется, пока сотрудник не примет согласие на ПДн в Telegram (статус awaiting_first)."
            )
        else:
            out["docs_survey_telegram_schedule_hint"] = (
                f"Напоминание в Telegram не отправляется: согласие ПДн — «{pd}» (нужно «accepted»)."
            )
        return out

    if docs_survey_reminder_30m_sent_at:  # truthy
        sent = utc_naive_to_local_display(docs_survey_reminder_30m_sent_at)
        out["docs_survey_reminder_telegram_local_label"] = sent
        out["docs_survey_telegram_schedule_hint"] = (
            f"Напоминание уже отправлено ({sent}). Слот: {slot_local}."
        )
        return out

    if sched_utc is None:
        return out
    rem_utc = sched_utc - timedelta(minutes=n)
    rem_label = aware_utc_to_local_label(rem_utc)
    out["docs_survey_reminder_telegram_local_label"] = rem_label
    out["docs_survey_minutes_until_reminder"] = _minutes_until_utc(rem_utc)

    out["docs_survey_telegram_schedule_hint"] = (
        f"Напоминание за {n} мин до слота: ориентировочно {rem_label}. Слот: {slot_local} ({tz_name})."
    )
    return out
