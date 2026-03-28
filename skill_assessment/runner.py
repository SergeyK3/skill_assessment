# route: GET /skill-assessment | file: skill_assessment/runner.py
"""
Точка входа ASGI: приложение ядра typical_infrastructure + роутер skill_assessment.

Запуск **из любого cwd**, если задан корень ядра::

    set TYPICAL_INFRA_ROOT=D:\\path\\to\\typical_infrastructure
    uvicorn skill_assessment.runner:app --reload --host 127.0.0.1 --port 8000

Либо из **корня клона ядра** (там же лежит пакет ``app``), чтобы он попал в PYTHONPATH::

    cd D:\\path\\to\\typical_infrastructure
    .venv\\Scripts\\activate
    pip install -e D:\\path\\to\\skill_assessment
    uvicorn skill_assessment.runner:app --reload --host 127.0.0.1 --port 8000

    Либо: ``python -m skill_assessment`` (тот же хост/порт).

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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from starlette.requests import Request

from skill_assessment.env import PLUGIN_ENV_FILE as _ENV_FILE
from skill_assessment.env import load_plugin_env

# .env до импорта db_models: bootstrap читает TYPICAL_INFRA_ROOT из .env.
# Уже выставленные переменные окружения не перетираем — это нужно для тестов и внешнего запуска.
load_plugin_env(override=False)

from skill_assessment.bootstrap import (
    _discover_typical_infra_root_dir,
    ensure_typical_infra_working_directory,
    ensure_typical_infrastructure_on_path,
)

ensure_typical_infrastructure_on_path()
ensure_typical_infra_working_directory()

import skill_assessment.infrastructure.db_models  # noqa: F401 — таблицы на общем Base

from app.db import SessionLocal

from skill_assessment.services.examination_seed import ensure_examination_questions
from skill_assessment.services.competency_seed import ensure_competency_matrix_seed
from skill_assessment.services.kpi_seed import ensure_kpi_matrix_seed
from skill_assessment.services.taxonomy_seed import ensure_demo_taxonomy

_log = logging.getLogger("skill_assessment")


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
    """SQLite: напоминание за N мин до слота (DOCS_SURVEY_REMINDER_MINUTES_BEFORE) и ответ «готов/не готов»."""
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


def _ensure_part1_docs_checklist_column(engine) -> None:
    """SQLite: JSON чек-листа «опрос по служебным документам» (Part 1)."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("sa_assessment_sessions")]
    except Exception:
        return
    if "part1_docs_checklist_json" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE sa_assessment_sessions ADD COLUMN part1_docs_checklist_json TEXT"))
    _log.info("skill_assessment: added column sa_assessment_sessions.part1_docs_checklist_json (migration)")


def _ensure_part1_docs_access_token_column(engine) -> None:
    """SQLite: токен личной страницы чек-листа (Part 1)."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("sa_assessment_sessions")]
    except Exception:
        return
    if "part1_docs_access_token" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE sa_assessment_sessions ADD COLUMN part1_docs_access_token VARCHAR(128)"))
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sa_sessions_part1_docs_access_token "
                    "ON sa_assessment_sessions (part1_docs_access_token)"
                )
            )
    except Exception:
        pass
    _log.info("skill_assessment: added column sa_assessment_sessions.part1_docs_access_token (migration)")


def _ensure_part2_cases_column(engine) -> None:
    """SQLite: JSON набора кейсов Part 2, ответов сотрудника и оценки ИИ."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("sa_assessment_sessions")]
    except Exception:
        return
    if "part2_cases_json" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE sa_assessment_sessions ADD COLUMN part2_cases_json TEXT"))
    _log.info("skill_assessment: added column sa_assessment_sessions.part2_cases_json (migration)")


def _ensure_manager_assessment_columns(engine) -> None:
    """SQLite: токен и метка уведомления для персональной страницы руководителя (Part 3)."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("sa_assessment_sessions")]
    except Exception:
        return
    with engine.begin() as conn:
        if "manager_access_token" not in cols:
            conn.execute(text("ALTER TABLE sa_assessment_sessions ADD COLUMN manager_access_token VARCHAR(128)"))
        if "manager_assessment_notified_at" not in cols:
            conn.execute(text("ALTER TABLE sa_assessment_sessions ADD COLUMN manager_assessment_notified_at DATETIME"))
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sa_sessions_manager_access_token "
                    "ON sa_assessment_sessions (manager_access_token)"
                )
            )
    except Exception:
        pass
    _log.info("skill_assessment: ensured manager assessment columns (migration)")


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


def _ensure_examination_question_scenario_id_column(engine) -> None:
    """SQLite: ссылка на набор вопросов (общий regulation_v1 или свой список по сессии/KPI)."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("sa_examination_sessions")]
    except Exception:
        return
    if "question_scenario_id" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE sa_examination_sessions ADD COLUMN question_scenario_id VARCHAR(64)"))
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_sa_examination_sessions_question_scenario_id "
                    "ON sa_examination_sessions (question_scenario_id)"
                )
            )
    except Exception:
        pass
    _log.info("skill_assessment: added column sa_examination_sessions.question_scenario_id (migration)")


def apply_skill_assessment_database_migrations() -> None:
    """
    ``create_all`` + ALTER для существующих SQLite БД (без Alembic).

    Вызывается из ``run_plugin_startup`` (uvicorn) и из ``telegram_worker``:
    отдельный процесс бота иначе не добавляет новые колонки → ``no such column`` при callback.
    """
    from app.db import Base, engine

    Base.metadata.create_all(bind=engine)
    _ensure_sa_session_phase_column(engine)
    _ensure_docs_survey_notify_chat_column(engine)
    _ensure_docs_survey_pd_consent_columns(engine)
    _ensure_docs_survey_consent_prompt_hr_columns(engine)
    _ensure_docs_survey_reminder_readiness_columns(engine)
    _ensure_part1_docs_checklist_column(engine)
    _ensure_part1_docs_access_token_column(engine)
    _ensure_part2_cases_column(engine)
    _ensure_manager_assessment_columns(engine)
    _ensure_examination_question_scenario_id_column(engine)
    _ensure_examination_access_token_column(engine)


# Роутер skill_assessment подключается в app.main (если пакет установлен), чтобы
# uvicorn app.main:app тоже отдавал /api/skill-assessment/*.

_static = Path(__file__).resolve().parent / "static"

_plugin_short_urls_registered = False


def _registered_paths(fastapi_app: FastAPI) -> set[str]:
    """Пути маршрутов верхнего уровня (для проверки дублей)."""
    out: set[str] = set()
    for r in fastapi_app.routes:
        p = getattr(r, "path", None)
        if isinstance(p, str):
            out.add(p)
    return out


def _register_global_ui_fallbacks(fastapi_app: FastAPI) -> None:
    """Если в загруженном ядре нет GET /global/… (старый клон), отдаём HTML из static ядра по TYPICAL_INFRA_ROOT."""
    root = _discover_typical_infra_root_dir()
    if root is None:
        _log.warning(
            "skill_assessment: не найден каталог ядра — задайте TYPICAL_INFRA_ROOT в .env плагина; "
            "страницы /global/* могут отдавать 404."
        )
        return
    static_dir = root / "static"
    if not static_dir.is_dir():
        _log.warning("skill_assessment: нет каталога static в ядре %s", root)
        return

    pairs: list[tuple[str, Path, str]] = [
        ("/global", static_dir / "global" / "index.html", "global_hub"),
        ("/global/template-org", static_dir / "global" / "template-org.html", "global_template_org"),
        ("/global/positions", static_dir / "global" / "positions.html", "global_positions"),
        ("/global/kpi-templates", static_dir / "global" / "kpi-templates.html", "global_kpi_templates"),
        ("/global/kpi", static_dir / "global" / "kpi-templates.html", "global_kpi_alias"),
        ("/global/matrix-skills", static_dir / "global" / "matrix-skills.html", "global_matrix_skills"),
        ("/global/matrix-kpi", static_dir / "global" / "matrix-kpi.html", "global_matrix_kpi"),
    ]
    have = _registered_paths(fastapi_app)
    for url_path, file_path, detail_key in pairs:
        if url_path in have:
            continue
        if not file_path.is_file():
            _log.warning(
                "skill_assessment: fallback UI пропущен %s — нет файла %s",
                url_path,
                file_path,
            )
            continue

        def _handler(fp: Path = file_path, dk: str = detail_key) -> FileResponse:
            if not fp.is_file():
                raise HTTPException(status_code=404, detail=dk + "_not_found")
            return FileResponse(fp)

        fastapi_app.add_api_route(
            url_path,
            _handler,
            methods=["GET"],
            include_in_schema=False,
        )
        _log.info(
            "skill_assessment: зарегистрирован fallback UI %s → %s (в ядре не было маршрута)",
            url_path,
            file_path,
        )


def _redirect_skill_assessment_ui(dest: str, request: Request) -> RedirectResponse:
    """Редирект на страницу плагина с сохранением query (например client_id из ядра)."""
    q = request.url.query
    url = dest + ("?" + q if q else "")
    return RedirectResponse(url=url, status_code=302)


def skill_assessment_page() -> FileResponse:
    """Входная страница плагина (лендинг; короткий URL /skill-assessment редиректит на /api/…/landing)."""
    p = _static / "index.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="skill_assessment_static_missing")
    return FileResponse(p)


def skill_assessment_future_demo_page() -> FileResponse:
    """Превью вымышленного сценария экзамена (LLM + кейс + оценка руководителя)."""
    p = _static / "demo_future_scenario.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="skill_assessment_demo_future_missing")
    return FileResponse(p)


def configure_skill_assessment_plugin(app: FastAPI) -> None:
    """
    Регистрирует короткие URL и статические GET до старта ASGI (lifespan).

    Нельзя вешать ``@app.middleware`` при импорте ``run_plugin_startup`` из lifespan:
    к этому моменту приложение уже «запущено» — Starlette запрещает add_middleware.
    Вызывайте из ``app.main`` сразу после сборки ``app`` (и include_router плагина).
    """
    global _plugin_short_urls_registered
    if _plugin_short_urls_registered:
        return
    _plugin_short_urls_registered = True

    @app.middleware("http")
    async def _skill_assessment_short_urls(request: Request, call_next):
        """Короткие URL → те же страницы под /api/skill-assessment/…"""
        p = request.url.path
        if p in ("/skill-assessment", "/skill-assessment/"):
            return _redirect_skill_assessment_ui("/api/skill-assessment/landing", request)
        if p in ("/skill-assessment/future-demo", "/skill-assessment/future-demo/"):
            return _redirect_skill_assessment_ui("/api/skill-assessment/demo/future-scenario", request)
        if p in ("/skill-assessment/part3", "/skill-assessment/part3/"):
            return _redirect_skill_assessment_ui("/api/skill-assessment/ui/part3", request)
        if p in ("/skill-assessment/part1", "/skill-assessment/part1/"):
            return _redirect_skill_assessment_ui("/api/skill-assessment/ui/part1", request)
        if p in ("/skill-assessment/part1-docs", "/skill-assessment/part1-docs/"):
            return _redirect_skill_assessment_ui("/api/skill-assessment/ui/part1-docs-checklist", request)
        if p in ("/skill-assessment/part2-case", "/skill-assessment/part2-case/"):
            return _redirect_skill_assessment_ui("/api/skill-assessment/ui/part2-case", request)
        if p in ("/skill-assessment/manager-assessment", "/skill-assessment/manager-assessment/"):
            return _redirect_skill_assessment_ui("/api/skill-assessment/ui/manager-assessment", request)
        return await call_next(request)

    app.add_api_route(
        "/skill-assessment",
        skill_assessment_page,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/skill-assessment/future-demo",
        skill_assessment_future_demo_page,
        methods=["GET"],
        include_in_schema=False,
    )


def run_plugin_startup() -> None:
    """Вызывается из lifespan ядра (app.main). Гарантируем таблицы и демо-таксономию."""
    # Повторно подхватить .env после сохранения файла (uvicorn --reload), не затирая уже заданное окружение.
    load_plugin_env(override=False)

    _log.info(
        "skill_assessment runner: плагин активен — GET /skill-assessment → /api/skill-assessment/landing, "
        "рабочий UI: GET /api/skill-assessment/workspace (GET /ui → 307 на workspace), health: GET /api/skill-assessment/health"
    )
    _log.info(
        "skill_assessment: в браузере используйте http://127.0.0.1:8000/api/skill-assessment/health "
        "(не localhost — на Windows возможен другой процесс на IPv6). "
        "Проверка порта: netstat -ano | findstr :8000"
    )
    apply_skill_assessment_database_migrations()
    db = SessionLocal()
    try:
        ensure_demo_taxonomy(db)
        ensure_examination_questions(db)
        ensure_competency_matrix_seed(db)
        ensure_kpi_matrix_seed(db)
        db.commit()
    finally:
        db.close()

    try:
        from skill_assessment.services.docs_survey_consent_timeout import start_consent_timeout_background_task

        start_consent_timeout_background_task()
        _log.info("skill_assessment: фоновая проверка таймаутов (ПДн + ответы экзамена) запущена.")
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


def get_app() -> FastAPI:
    """Для ``uvicorn skill_assessment.runner:app`` — одно приложение с ядром (configure вызывается из app.main)."""
    import app.main as app_main_mod

    _log.info("skill_assessment: загружено ядро app.main из %s", getattr(app_main_mod, "__file__", "?"))
    # Дублирует вызов из configure_skill_assessment_plugin — idempotent: страницы /global/* из TYPICAL_INFRA_ROOT,
    # если в загруженном ядре нет маршрута (старый клон или configure не вызвался из-за ImportError).
    _register_global_ui_fallbacks(app_main_mod.app)
    return app_main_mod.app


app = get_app()
