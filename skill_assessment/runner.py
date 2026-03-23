# route: GET /skill-assessment | file: skill_assessment/runner.py
"""
Точка входа ASGI: приложение ядра typical_infrastructure + роутер skill_assessment.

Запускать из **корня клона ядра**, чтобы пакет ``app`` был в PYTHONPATH::

    cd D:\\path\\to\\typical_infrastructure
    .venv\\Scripts\\activate
    pip install -e D:\\path\\to\\skill_assessment
    uvicorn skill_assessment.runner:app --reload --host 127.0.0.1 --port 8000

    Либо из каталога ядра: ``python -m skill_assessment`` (тот же хост/порт).

    Если все пути плагина дают 404, а ``GET /health/ready`` от ядра открывается: на Windows
    в браузере открывайте **http://127.0.0.1:8000/...**, а не ``http://localhost:8000/...``
    — ``localhost`` может уйти на другой процесс по IPv6 (``::1``), а uvicorn слушает IPv4.
    Проверка: ``netstat -ano | findstr :8000`` (несколько LISTENING — разные стеки).

Ядро в upstream не меняется; зависимость на skill_assessment в requirements ядра не добавляется.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from starlette.requests import Request

import skill_assessment.infrastructure.db_models  # noqa: F401 — таблицы на общем Base

from app.db import SessionLocal
from app.main import app

from skill_assessment.router import router as skill_assessment_router
from skill_assessment.services.examination_seed import ensure_examination_questions
from skill_assessment.services.taxonomy_seed import ensure_demo_taxonomy

_log = logging.getLogger("skill_assessment")

# .env из корня пакета (рядом с pyproject.toml), даже если cwd = typical_infrastructure
_PKG_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PKG_ROOT / ".env"
# override=True: значения из .env перекрывают пустые/старые переменные окружения ОС
load_dotenv(_ENV_FILE, override=True)


def _install_httpx_telegram_token_redaction() -> None:
    """Не писать Bot API токен в логи (httpx логирует полный URL)."""

    class _RedactTelegramTokenFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                msg = record.getMessage()
                if "api.telegram.org/bot" in msg:
                    record.msg = re.sub(r"/bot[0-9]+:[A-Za-z0-9_-]+/", "/bot<redacted>/", msg)
                    record.args = ()
            except Exception:
                pass
            return True

    logging.getLogger("httpx").addFilter(_RedactTelegramTokenFilter())


_install_httpx_telegram_token_redaction()


def _ensure_sa_session_phase_column(engine) -> None:
    """SQLite: добавить колонку phase к существующим БД без Alembic."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("sa_assessment_sessions")]
    except Exception:
        return
    if "phase" in cols:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE sa_assessment_sessions ADD COLUMN phase VARCHAR(32) NOT NULL DEFAULT 'draft'"
            )
        )
    _log.info("skill_assessment: added column sa_assessment_sessions.phase (migration)")


def _ensure_docs_survey_notify_chat_column(engine) -> None:
    """SQLite: chat_id получателя первого уведомления Part1 (inline-календарь)."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("sa_assessment_sessions")]
    except Exception:
        return
    if "docs_survey_notify_chat_id" in cols:
        return
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE sa_assessment_sessions ADD COLUMN docs_survey_notify_chat_id VARCHAR(32)")
        )
    _log.info("skill_assessment: added column sa_assessment_sessions.docs_survey_notify_chat_id (migration)")


def _ensure_docs_survey_pd_consent_columns(engine) -> None:
    """SQLite: слот опроса и статус согласия на ПДн (Part1, Telegram)."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("sa_assessment_sessions")]
    except Exception:
        return
    with engine.begin() as conn:
        if "docs_survey_scheduled_at" not in cols:
            conn.execute(text("ALTER TABLE sa_assessment_sessions ADD COLUMN docs_survey_scheduled_at DATETIME"))
        if "docs_survey_pd_consent_status" not in cols:
            conn.execute(
                text("ALTER TABLE sa_assessment_sessions ADD COLUMN docs_survey_pd_consent_status VARCHAR(16)")
            )
        if "docs_survey_pd_consent_at" not in cols:
            conn.execute(text("ALTER TABLE sa_assessment_sessions ADD COLUMN docs_survey_pd_consent_at DATETIME"))
    _log.info("skill_assessment: ensured docs_survey PD consent columns (migration)")


def _ensure_docs_survey_consent_prompt_hr_columns(engine) -> None:
    """SQLite: время первого запроса согласия и факт уведомления HR (таймаут 10 мин)."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("sa_assessment_sessions")]
    except Exception:
        return
    with engine.begin() as conn:
        if "docs_survey_consent_prompt_sent_at" not in cols:
            conn.execute(
                text("ALTER TABLE sa_assessment_sessions ADD COLUMN docs_survey_consent_prompt_sent_at DATETIME")
            )
        if "docs_survey_hr_notified_no_consent_at" not in cols:
            conn.execute(
                text("ALTER TABLE sa_assessment_sessions ADD COLUMN docs_survey_hr_notified_no_consent_at DATETIME")
            )
    _log.info("skill_assessment: ensured docs_survey consent prompt / HR notify columns (migration)")


def _ensure_docs_survey_reminder_readiness_columns(engine) -> None:
    """SQLite: напоминание за 30 мин и ответ «готов/не готов»."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("sa_assessment_sessions")]
    except Exception:
        return
    with engine.begin() as conn:
        if "docs_survey_reminder_30m_sent_at" not in cols:
            conn.execute(
                text("ALTER TABLE sa_assessment_sessions ADD COLUMN docs_survey_reminder_30m_sent_at DATETIME")
            )
        if "docs_survey_readiness_answer" not in cols:
            conn.execute(
                text("ALTER TABLE sa_assessment_sessions ADD COLUMN docs_survey_readiness_answer VARCHAR(16)")
            )
        if "docs_survey_exam_gate_awaiting" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE sa_assessment_sessions ADD COLUMN docs_survey_exam_gate_awaiting "
                    "BOOLEAN NOT NULL DEFAULT 0"
                )
            )
    _log.info("skill_assessment: ensured docs_survey reminder / readiness columns (migration)")


def _ensure_examination_access_token_column(engine) -> None:
    """SQLite: колонка access_token для персональных ссылок на экзамен (без Alembic)."""
    from sqlalchemy import inspect, or_, select, text

    from skill_assessment.infrastructure.db_models import ExaminationSessionRow

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("sa_examination_sessions")]
    except Exception:
        return
    if "access_token" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE sa_examination_sessions ADD COLUMN access_token VARCHAR(128)"))
        _log.info("skill_assessment: added column sa_examination_sessions.access_token (migration)")

    db = SessionLocal()
    try:
        rows = db.scalars(
            select(ExaminationSessionRow).where(
                or_(
                    ExaminationSessionRow.access_token.is_(None),
                    ExaminationSessionRow.access_token == "",
                )
            )
        ).all()
        for r in rows:
            r.access_token = secrets.token_urlsafe(32)
        if rows:
            db.commit()
            _log.info(
                "skill_assessment: backfilled access_token for %d examination session(s)",
                len(rows),
            )
    finally:
        db.close()

    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sa_examination_sessions_access_token "
                    "ON sa_examination_sessions (access_token)"
                )
            )
    except Exception:
        pass


app.include_router(skill_assessment_router, prefix="/api")

_static = Path(__file__).resolve().parent / "static"


def _redirect_skill_assessment_ui(dest: str, request: Request) -> RedirectResponse:
    """Редирект на страницу плагина с сохранением query (например client_id из ядра)."""
    q = request.url.query
    url = dest + ("?" + q if q else "")
    return RedirectResponse(url=url, status_code=302)


@app.middleware("http")
async def _skill_assessment_short_urls(request: Request, call_next):
    """Короткие URL → те же страницы под /api/skill-assessment/… (надёжнее, чем только декораторы на app)."""
    p = request.url.path
    if p in ("/skill-assessment", "/skill-assessment/"):
        return _redirect_skill_assessment_ui("/api/skill-assessment/landing", request)
    if p in ("/skill-assessment/future-demo", "/skill-assessment/future-demo/"):
        return _redirect_skill_assessment_ui("/api/skill-assessment/demo/future-scenario", request)
    if p in ("/skill-assessment/part3", "/skill-assessment/part3/"):
        return _redirect_skill_assessment_ui("/api/skill-assessment/ui/part3", request)
    if p in ("/skill-assessment/part1", "/skill-assessment/part1/"):
        return _redirect_skill_assessment_ui("/api/skill-assessment/ui/part1", request)
    return await call_next(request)


@app.on_event("startup")
async def _skill_assessment_startup() -> None:
    """Гарантируем таблицы skill_assessment и демо-таксономию при пустых таблицах."""
    # Повторно подхватить .env после сохранения файла (uvicorn --reload)
    load_dotenv(_ENV_FILE, override=True)

    _log.info(
        "skill_assessment runner: плагин активен — GET /skill-assessment → /api/skill-assessment/landing, "
        "рабочий UI: GET /api/skill-assessment/workspace (GET /ui → 307 на workspace), health: GET /api/skill-assessment/health"
    )
    _log.info(
        "skill_assessment: в браузере используйте http://127.0.0.1:8000/api/skill-assessment/health "
        "(не localhost — на Windows возможен другой процесс на IPv6). "
        "Проверка порта: netstat -ano | findstr :8000"
    )
    from app.db import Base, engine

    Base.metadata.create_all(bind=engine)
    _ensure_sa_session_phase_column(engine)
    _ensure_docs_survey_notify_chat_column(engine)
    _ensure_docs_survey_pd_consent_columns(engine)
    _ensure_docs_survey_consent_prompt_hr_columns(engine)
    _ensure_docs_survey_reminder_readiness_columns(engine)
    _ensure_examination_access_token_column(engine)
    db = SessionLocal()
    try:
        ensure_demo_taxonomy(db)
        ensure_examination_questions(db)
        db.commit()
    finally:
        db.close()

    try:
        from skill_assessment.services.docs_survey_consent_timeout import start_consent_timeout_background_task

        start_consent_timeout_background_task()
        _log.info("skill_assessment: фоновая проверка таймаута согласия ПДн (10 мин) запущена.")
    except Exception:
        _log.exception("skill_assessment: не удалось запустить проверку таймаута согласия ПДн")

    raw_poll = os.getenv("TELEGRAM_ENABLE_POLLING", "")
    poll = raw_poll.strip().lower() in ("1", "true", "yes")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    raw_embed = os.getenv("TELEGRAM_POLLING_RUN_IN_UVICORN", "1")
    run_poll_in_uvicorn = raw_embed.strip().lower() in ("1", "true", "yes")
    _log.info(
        "skill_assessment: .env=%s TELEGRAM_ENABLE_POLLING=%r → polling=%s, token_set=%s, "
        "TELEGRAM_POLLING_RUN_IN_UVICORN=%r → embedded=%s",
        _ENV_FILE,
        raw_poll,
        poll,
        bool(token),
        raw_embed,
        run_poll_in_uvicorn,
    )
    if poll and token and run_poll_in_uvicorn:
        from skill_assessment.integration.telegram_poller import start_background_polling
        import skill_assessment.telegram_runtime as tg_rt

        start_background_polling(token)
        tg_rt.polling_started = True
        _log.info(
            "skill_assessment: Telegram long polling внутри uvicorn — отправьте /start боту в Telegram."
        )
    elif poll and token and not run_poll_in_uvicorn:
        _log.info(
            "skill_assessment: long polling в uvicorn отключён (TELEGRAM_POLLING_RUN_IN_UVICORN=0). "
            "Запустите бота отдельно из каталога ядра: "
            "python -m skill_assessment.telegram_worker"
        )
    elif poll and not token:
        _log.warning("skill_assessment: TELEGRAM_ENABLE_POLLING=1, но TELEGRAM_BOT_TOKEN пуст — polling не запущен.")


@app.get("/skill-assessment", include_in_schema=False)
def skill_assessment_page() -> FileResponse:
    """Входная страница плагина (лендинг; короткий URL /skill-assessment редиректит на /api/…/landing)."""
    p = _static / "index.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="skill_assessment_static_missing")
    return FileResponse(p)


@app.get("/skill-assessment/future-demo", include_in_schema=False)
def skill_assessment_future_demo_page() -> FileResponse:
    """Превью вымышленного сценария экзамена (LLM + кейс + оценка руководителя)."""
    p = _static / "demo_future_scenario.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="skill_assessment_demo_future_missing")
    return FileResponse(p)
