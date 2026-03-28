# route: (cli) | file: skill_assessment/telegram_worker.py
"""
Отдельный процесс: только Telegram long polling (без uvicorn).

Зачем: при ``uvicorn --reload`` дочерний процесс API перезапускается и на короткое время
может существовать два вызова ``getUpdates`` с одним токеном → Telegram отвечает **409 Conflict**.

Рекомендуемая схема (один компьютер, два терминала):

1. В ``.env`` пакета skill_assessment::

     TELEGRAM_ENABLE_POLLING=1
     TELEGRAM_POLLING_RUN_IN_UVICORN=0

2. Терминал A — API::

     cd path/to/typical_infrastructure
     .venv\\Scripts\\activate
     uvicorn skill_assessment.runner:app --reload --host 127.0.0.1 --port 8000

3. Терминал B — только бот::

     cd path/to/typical_infrastructure
     .venv\\Scripts\\activate
     python -m skill_assessment.telegram_worker

В этом процессе также крутится тот же фоновый цикл, что и в uvicorn: таймаут согласия ПДн (10 мин)
и **напоминание в Telegram за N минут до слота** опроса по документам. Без этого цикла напоминания
не уходят, если API не запущен.

Перезапускайте воркер бота вручную при смене кода polling (или держите его без reload).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from skill_assessment.env import load_plugin_env

_pkg_parent = Path(__file__).resolve().parent.parent
load_plugin_env(override=False)
# До ensure: чтобы предупреждения bootstrap про TYPICAL_INFRA_ROOT попали в консоль
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from skill_assessment.bootstrap import ensure_typical_infra_working_directory

# Тот же SQLite, что у uvicorn: cwd влияет на app.settings / app.db
ensure_typical_infra_working_directory()

# Таблицы на общем Base ядра (как в runner); bootstrap читает TYPICAL_INFRA_ROOT из .env
import skill_assessment.infrastructure.db_models  # noqa: F401

_log = logging.getLogger("skill_assessment.telegram_worker")


def main() -> None:
    env_file = _pkg_parent / ".env"

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or len(token) < 10:
        _log.error("TELEGRAM_BOT_TOKEN пуст или слишком короткий — проверьте %s", env_file)
        sys.exit(1)

    _log.info(
        "telegram_worker: отдельный процесс long polling; .env=%s cwd=%s "
        "(API должен быть с TELEGRAM_POLLING_RUN_IN_UVICORN=0)",
        env_file,
        os.getcwd(),
    )
    try:
        from app.settings import settings as _infra_settings

        _log.info(
            "telegram_worker: SQLite ядра — sqlite_path=%r (тот же файл, что у uvicorn при том же TYPICAL_INFRA_ROOT)",
            getattr(_infra_settings, "sqlite_path", ""),
        )
    except Exception:
        pass
    # Те же миграции SQLite, что при старте uvicorn — иначе в БД нет новых колонок (part1_docs_* и др.).
    from skill_assessment.runner import apply_skill_assessment_database_migrations

    apply_skill_assessment_database_migrations()

    from app.db import SessionLocal

    from skill_assessment.services.competency_seed import ensure_competency_matrix_seed
    from skill_assessment.services.kpi_seed import ensure_kpi_matrix_seed

    _db = SessionLocal()
    try:
        ensure_competency_matrix_seed(_db)
        ensure_kpi_matrix_seed(_db)
        _db.commit()
    except Exception:
        _db.rollback()
        raise
    finally:
        _db.close()

    from skill_assessment.integration.telegram_poller import run_long_polling
    from skill_assessment.services.docs_survey_consent_timeout import run_docs_survey_consent_timeout_loop

    async def _run_bot_and_schedulers() -> None:
        _log.info(
            "telegram_worker: long polling + фоновые проверки (согласие ПДн, таймаут ответов экзамена, напоминание за N мин до слота)"
        )
        await asyncio.gather(
            run_long_polling(token),
            run_docs_survey_consent_timeout_loop(),
        )

    asyncio.run(_run_bot_and_schedulers())


if __name__ == "__main__":
    main()
