<!-- route: (documentation) | file: docs/README.md -->

# skill-assessment

Отдельный пакет: API и черновой UI **оценки навыков**, подключаемые к [typical_infrastructure](https://github.com/SergeyK3/typical_infrastructure) **v1.0.0** без изменений в upstream-ядре и **без** записи в `requirements.txt` ядра.

**Другие материалы в этой папке:** [TESTING.md](TESTING.md) — стратегия тестирования; [EXAM_SCENARIO.md](EXAM_SCENARIO.md) — сценарий экзамена; [EXAM_OPEN_QUESTIONS.md](EXAM_OPEN_QUESTIONS.md) — пробелы и вводные вопросы перед реализацией в коде; [SUMMIT_FOLLOWUP.md](SUMMIT_FOLLOWUP.md) — краткая сводка после саммита (что сделано, что коммитить).

## Как это устроено

- Импортируется готовое приложение FastAPI из ядра: `from app.main import app`.
- К нему добавляется роутер плагина и маршрут `GET /skill-assessment` (статика из этого пакета).
- Запуск: **`uvicorn skill_assessment.runner:app`**, рабочий каталог — **корень клона ядра** (чтобы пакет `app` находился по `PYTHONPATH`).

## Установка

```powershell
cd D:\path\to\typical_infrastructure
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e D:\path\to\skill_assessment
```

Скопируйте **`.env.example`** в **`.env`** в корне пакета `skill_assessment` и при необходимости задайте:

| Переменная | Назначение |
|------------|------------|
| `TELEGRAM_BOT_TOKEN` | Токен от @BotFather (dev-бот отдельно от прода). |
| `TELEGRAM_ENABLE_POLLING` | `1` — при старте uvicorn запускается **long polling**; бот отвечает на `/start` (удобно в разработке). В тестах и CI не включать. |

При старте `runner` подхватывает **`.env`** из корня пакета (даже если текущий каталог — ядро `typical_infrastructure`).

## Запуск

```powershell
cd D:\path\to\typical_infrastructure
.\.venv\Scripts\Activate.ps1
uvicorn skill_assessment.runner:app --host 0.0.0.0 --port 8000 --reload
```

Или скрипт из корня пакета (подставьте пути):

```powershell
.\run_http.ps1 -CoreRoot "D:\path\to\typical_infrastructure" -SkillPkgRoot "D:\path\to\skill_assessment"
```

## API

| Метод | Путь |
|--------|------|
| GET | `/api/skill-assessment/health` |
| GET | `/api/skill-assessment/telegram/debug` — без токена: `.env`, polling, webhook |
| GET | `/api/skill-assessment/domain/json-schema` |
| GET | `/api/skill-assessment/taxonomy/domains` |
| GET | `/api/skill-assessment/taxonomy/skills?domain_id=` |
| POST | `/api/skill-assessment/sessions` |
| GET | `/api/skill-assessment/sessions` |
| GET | `/api/skill-assessment/sessions/{id}` |
| POST | `/api/skill-assessment/sessions/{id}/start` |
| POST | `/api/skill-assessment/sessions/{id}/cancel` — отмена незавершённого назначения (`cancelled`) |
| POST | `/api/skill-assessment/sessions/{id}/complete` |
| POST | `/api/skill-assessment/sessions/{id}/results` |
| GET | `/api/skill-assessment/sessions/{id}/results` |
| GET | `/skill-assessment` — черновой UI |

**Экзамен по регламентам (сценарий `regulation_v1`, отдельно от Part1/2/3):**

| Метод | Путь |
|--------|------|
| POST | `/api/skill-assessment/examination/sessions` |
| GET | `/api/skill-assessment/examination/sessions` |
| GET | `/api/skill-assessment/examination/sessions/by-access-token/{token}` — персональная веб-ссылка (секрет из ответа POST) |
| GET | `/api/skill-assessment/examination/sessions/{id}` |
| GET | `/api/skill-assessment/examination/scenarios/{scenario_id}/questions` |
| GET | `/api/skill-assessment/examination/sessions/{id}/current-question` |
| POST | `/api/skill-assessment/examination/sessions/{id}/consent` |
| POST | `/api/skill-assessment/examination/sessions/{id}/hr/release-consent-block` |
| POST | `/api/skill-assessment/examination/sessions/{id}/intro/done` |
| POST | `/api/skill-assessment/examination/sessions/{id}/answer` |
| GET | `/api/skill-assessment/examination/sessions/{id}/protocol` |
| POST | `/api/skill-assessment/examination/sessions/{id}/complete` |
| POST | `/api/skill-assessment/examination/telegram/bindings` — привязка `telegram_chat_id` к `client_id` + `employee_id` |

Таблицы SQLite: префикс `sa_*`, общий файл с ядром (`app.db` или из `SQLITE_PATH`). Экзамен: `sa_examination_questions`, `sa_examination_sessions`, `sa_examination_answers`, `sa_examination_telegram_bindings`.

## Черновая модель (domain + ORM)

- Pydantic: `skill_assessment/domain/entities.py` (оценка навыков), `skill_assessment/domain/examination_entities.py` (экзамен по регламентам)
- SQLAlchemy: `skill_assessment/infrastructure/db_models.py` (общий `Base` ядра)

При первом запуске поднимается демо-таксономия (один домен COMM и два навыка), если таблицы пустые; для экзамена — фиксированные вопросы `regulation_v1` в `sa_examination_questions`, если пусто.

## Тесты

Стратегия проверки (экзамен, веб, Telegram, моки каналов): см. **[TESTING.md](TESTING.md)**.

Из venv ядра, с установленным `skill_assessment[dev]`::

```powershell
pytest D:\path\to\repo\skill_assessment\tests\test_assessment_flow.py D:\path\to\repo\skill_assessment\tests\test_examination_flow.py D:\path\to\repo\skill_assessment\tests\test_examination_telegram.py -q
```

`conftest.py` добавляет `typical_infrastructure` в `sys.path`. Для `TestClient` используйте контекст `with TestClient(app) as c:` — так отрабатывает startup и создаются таблицы.

## Git

Первый коммит в корне пакета; ветка по умолчанию — `main`. Подключение к GitHub: `git remote add origin …`, затем `git push -u origin main`.

Swagger ядра: `/docs` (эндпоинты плагина попадут в ту же схему).

## Интеграция с ядром без правок upstream (детализация)

Ниже — варианты, **если не хотите менять** репозиторий ядра на GitHub.

### A. Composition (текущий подход)

Один процесс: `runner` импортирует `app` ядра и дополняет его. Upstream ядра не трогается. Деплой: в окружении должны быть установлены ядро (как сейчас) и `pip install -e skill_assessment`, команда запуска — `uvicorn skill_assessment.runner:app`.

**Минус:** точка входа не `uvicorn app.main:app`, а `runner` — это нужно зафиксировать в runbook/compose.

### B. Форк ядра с минимальным патчем

Если принципиально нужен стандартный `uvicorn app.main:app`, в **форке** ядра добавляют 2–5 строк в `app/api.py` (импорт и `include_router`). Пакет `skill_assessment` остаётся отдельным репозиторием и ставится `pip install -e` или из Git. Upstream синхронизируете rebase/merge с тегом v1.0.x.

**Плюс:** привычная команда запуска. **Минус:** поддержка форка.

### C. Патч-файл к релизу (git apply)

Храните в репозитории `skill_assessment` файл `patches/v1.0.0-core-include-router.patch`. При деплое: клон ядра на тег → `git apply` → установка пакета. Без постоянного форка, но шаг ручной/CI.

### D. Два сервиса + reverse proxy

Ядро на `:8000`, skill_assessment как отдельный ASGI на `:8001` с собственным `app`; nginx маршрутизирует `/api/skill-assessment` и `/skill-assessment` на второй сервис. **Плюс:** полная изоляция. **Минус:** CORS, двойной деплой, общая сессия/auth сложнее.

### E. Пункт «Приложения» в UI ядра

Сайдбар рабочего пространства (`static/workspace/index.html`) живёт в ядре. Без изменения HTML ссылку на плагин пользователь открывает вручную (`/skill-assessment`) или через закладки. Чтобы пункт появился в меню, нужен **любой** из: маленький патч в форке; `git apply`; генерация статики при сборке образа.

---

Публикация на GitHub — по готовности черновика; зависимость на ядро в `pyproject` можно оформить как комментарий + документация или как `dependency_links` / прямой URL на тег после выкладки.
