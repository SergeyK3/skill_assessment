# route: (bootstrap) | file: skill_assessment/bootstrap.py
"""Добавляет корень ядра typical_infrastructure в sys.path, чтобы работал ``import app``."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from skill_assessment.env import load_env_file, load_plugin_env

_ran = False
_log = logging.getLogger(__name__)


def _prepend_infra_root(p: Path) -> None:
    """Поставить корень ядра первым в sys.path (убрать дубликат, если был ниже)."""
    s = str(p.resolve())
    if s in sys.path:
        sys.path.remove(s)
    sys.path.insert(0, s)


def ensure_typical_infrastructure_on_path() -> None:
    """
    Пакет ``app`` живёт в отдельном репозитории (typical_infrastructure).

    Порядок поиска:

    1. Переменные ``TYPICAL_INFRA_ROOT`` / ``SKILL_ASSESSMENT_CORE_ROOT`` (из ОС и ``.env`` плагина)
       — **всегда** в начало ``sys.path``, иначе ``import app`` может взять **другой** клон из PYTHONPATH.
    2. Эвристика: ``…/typical_infrastructure`` при обходе вверх от каталога пакета.
    3. Иначе — как получится: ``import app`` из текущего path.
    """
    global _ran
    if _ran:
        return
    _ran = True

    try:
        load_plugin_env(override=False)
    except Exception:
        pass

    for key in ("TYPICAL_INFRA_ROOT", "SKILL_ASSESSMENT_CORE_ROOT"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        p = Path(raw).expanduser().resolve()
        if p.is_dir() and (p / "app").is_dir():
            _prepend_infra_root(p)
            _log.info("skill_assessment: ядро из %s=%s — первым в sys.path", key, p)
            try:
                import app  # noqa: F401
            except ModuleNotFoundError:
                _log.warning("skill_assessment: после %s=%s пакет app не импортируется", key, p)
            return

    here = Path(__file__).resolve().parent
    cur = here.parent
    for _ in range(10):
        cand = cur / "typical_infrastructure"
        if cand.is_dir() and (cand / "app").is_dir():
            _prepend_infra_root(cand.resolve())
            _log.info("skill_assessment: ядро обнаружено рядом с пакетом: %s", cand.resolve())
            try:
                import app  # noqa: F401
            except ModuleNotFoundError:
                _log.warning("skill_assessment: после эвристики %s пакет app не импортируется", cand)
            return
        if cur.parent == cur:
            break
        cur = cur.parent

    try:
        import app  # noqa: F401
    except ModuleNotFoundError:
        _log.warning(
            "skill_assessment: пакет app не найден — задайте TYPICAL_INFRA_ROOT на каталог с папкой app/."
        )


def _discover_typical_infra_root_dir() -> Path | None:
    """Каталог ядра с папкой ``app/``: сначала ``TYPICAL_INFRA_ROOT``, иначе обход вверх от пакета."""
    for key in ("TYPICAL_INFRA_ROOT", "SKILL_ASSESSMENT_CORE_ROOT"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        p = Path(raw).expanduser().resolve()
        if p.is_dir() and (p / "app").is_dir():
            return p
    here = Path(__file__).resolve().parent
    cur = here.parent
    for _ in range(12):
        cand = cur / "typical_infrastructure"
        if cand.is_dir() and (cand / "app").is_dir():
            return cand.resolve()
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def ensure_typical_infra_working_directory() -> None:
    """
    Ядро (``app.settings``) подхватывает ``env_file=".env"`` и путь SQLite относительно **cwd**.

    Если API запущен из каталога ``typical_infrastructure``, а ``python -m skill_assessment.telegram_worker``
    — из другого каталога (например из клона плагина), открываются **разные** файлы ``app.db``:
    сессия создаётся в одной БД, бот читает другую → «Сессия не найдена» на кнопках Да/Нет.

    После ``load_dotenv`` пакета skill_assessment выставляем cwd на ``TYPICAL_INFRA_ROOT`` и
    подмешиваем ``.env`` ядра с ``override=False`` (токены плагина не перезаписываются).
    """
    raw = os.environ.get("TYPICAL_INFRA_ROOT", "").strip()
    p = Path(raw).expanduser().resolve() if raw else None
    if raw and p is not None and (not p.is_dir() or not (p / "app").is_dir()):
        _log.warning(
            "skill_assessment: TYPICAL_INFRA_ROOT=%r не существует или нет app/ — "
            "ищем typical_infrastructure рядом с пакетом (иначе SQLite ≠ API → «Сессия не найдена»).",
            raw,
        )
        p = None
    if p is None or not p.is_dir():
        discovered = _discover_typical_infra_root_dir()
        if discovered is None:
            _log.warning(
                "skill_assessment: не удалось перейти в каталог ядра — cwd=%s остаётся прежним; "
                "задайте существующий TYPICAL_INFRA_ROOT в .env (каталог с папкой app/).",
                os.getcwd(),
            )
            return
        p = discovered
        _log.info("skill_assessment: cwd ядра (обнаружен автоматически): %s", p)
    infra_env = p / ".env"
    if infra_env.is_file():
        load_env_file(infra_env, override=False)
    os.chdir(p)
