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

Перезапускайте воркер бота вручную при смене кода polling (или держите его без reload).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Таблицы на общем Base ядра (как в runner)
import skill_assessment.infrastructure.db_models  # noqa: F401

_log = logging.getLogger("skill_assessment.telegram_worker")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    pkg_parent = Path(__file__).resolve().parent.parent
    env_file = pkg_parent / ".env"
    load_dotenv(env_file, override=True)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or len(token) < 10:
        _log.error("TELEGRAM_BOT_TOKEN пуст или слишком короткий — проверьте %s", env_file)
        sys.exit(1)

    _log.info(
        "telegram_worker: отдельный процесс long polling; .env=%s "
        "(API должен быть с TELEGRAM_POLLING_RUN_IN_UVICORN=0)",
        env_file,
    )
    from skill_assessment.integration.telegram_poller import run_long_polling

    asyncio.run(run_long_polling(token))


if __name__ == "__main__":
    main()
