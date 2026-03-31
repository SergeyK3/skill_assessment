"""Smoke-check полного маршрута без ручной проверки разрывов между этапами.

Запуск:
    python scripts/route_gap_smoke_check.py

Что проверяет:
1) После завершения интервью (5 ответов) сразу уходят сообщения Part 2 в Telegram (в тот же чат).
2) После отправки ответов по кейсам сразу уходит уведомление руководителю (Part 3 ready).
3) После оценки руководителя сотруднику уходит обновление общего протокола.
"""

from __future__ import annotations

import os
import re
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient


def _must(cond: bool, msg: str) -> None:
    if not cond:
        raise RuntimeError(msg)


def _preflight_telegram_http_payload_contract() -> None:
    """
    Контракт outbound-адаптера:
    - reply_markup=None не должен попадать в JSON payload;
    - reply_markup=dict должен отправляться как объект.
    """
    from skill_assessment.adapters.telegram_outbound import HttpxTelegramOutbound

    captured: list[dict] = []

    class _Resp:
        status_code = 200
        is_success = True
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"ok": True}

    def _fake_post(url, json, timeout):  # noqa: ANN001
        captured.append(json)
        return _Resp()

    with patch("httpx.post", _fake_post):
        out = HttpxTelegramOutbound()
        r1 = out.send_message(token="123:abc", chat_id="1", text="ping", reply_markup=None)
        _must(r1.ok, "preflight outbound send failed for reply_markup=None")
        kb = {"inline_keyboard": [[{"text": "Да", "callback_data": "ok"}]]}
        r2 = out.send_message(token="123:abc", chat_id="1", text="ping2", reply_markup=kb)
        _must(r2.ok, "preflight outbound send failed for reply_markup=dict")

    _must(len(captured) == 2, "preflight outbound: expected 2 captured payloads")
    _must("reply_markup" not in captured[0], "preflight outbound: reply_markup=None leaked into JSON")
    _must(captured[1].get("reply_markup"), "preflight outbound: reply_markup object not present")
    print("[OK] outbound payload contract: reply_markup serialized correctly")


def main() -> None:
    # Тест без сети: все Telegram-сообщения пишутся в in-memory очередь.
    os.environ["SKILL_ASSESSMENT_TELEGRAM_OUTBOUND"] = "mock"
    os.environ["SKILL_ASSESSMENT_PUBLIC_BASE_URL"] = "https://example.test"
    os.environ["TELEGRAM_EXAM_MANAGER_CHAT_ID"] = "mgr_chat_001"

    from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
    from skill_assessment.runner import app

    outbound = get_telegram_outbound()
    outbound.clear()

    suffix = uuid.uuid4().hex[:8]
    client_id = f"c_gap_{suffix}"
    employee_id = f"e_gap_{suffix}"
    telegram_chat_id = "9" + str(int(uuid.uuid4().int % 10_000_000_000)).zfill(10)

    print("== route_gap_smoke_check ==")
    print(f"client_id={client_id} employee_id={employee_id} chat_id={telegram_chat_id}")
    _preflight_telegram_http_payload_contract()

    with TestClient(app) as client:
        # Привязка Telegram сотрудника (чтобы кейсы точно ушли в этот чат).
        r = client.post(
            "/api/skill-assessment/examination/telegram/bindings",
            json={
                "client_id": client_id,
                "employee_id": employee_id,
                "telegram_chat_id": telegram_chat_id,
            },
        )
        _must(r.status_code == 200, f"bind failed: {r.status_code} {r.text}")

        # Единая сессия оценки.
        r = client.post("/api/skill-assessment/sessions", json={"client_id": client_id, "employee_id": employee_id})
        _must(r.status_code == 200, f"create assessment failed: {r.status_code} {r.text}")
        session_id = r.json()["id"]

        r = client.post(f"/api/skill-assessment/sessions/{session_id}/start")
        _must(r.status_code == 200, f"start assessment failed: {r.status_code} {r.text}")
        token = r.json().get("part1_docs_checklist_token")
        _must(bool(token), "part1_docs_checklist_token missing after start")

        # Экзамен по регламентам.
        outbound.clear()
        r = client.post(
            "/api/skill-assessment/examination/sessions",
            json={"client_id": client_id, "employee_id": employee_id, "scenario_id": "regulation_v1"},
        )
        _must(r.status_code == 200, f"create exam failed: {r.status_code} {r.text}")
        exam_id = r.json()["id"]

        r = client.post(f"/api/skill-assessment/examination/sessions/{exam_id}/consent", json={"accepted": True})
        _must(r.status_code == 200, f"exam consent failed: {r.status_code} {r.text}")
        r = client.post(f"/api/skill-assessment/examination/sessions/{exam_id}/intro/done")
        _must(r.status_code == 200, f"exam intro failed: {r.status_code} {r.text}")
        for i in range(5):
            r = client.post(
                f"/api/skill-assessment/examination/sessions/{exam_id}/answer",
                json={"transcript_text": f"Ответ {i + 1}"},
            )
            _must(r.status_code == 200, f"exam answer#{i+1} failed: {r.status_code} {r.text}")
        r = client.post(f"/api/skill-assessment/examination/sessions/{exam_id}/complete")
        _must(r.status_code == 200, f"exam complete failed: {r.status_code} {r.text}")

        # Проверка 1: после завершения интервью сразу ушли кейсы.
        after_exam = [m for m in outbound.messages if m.get("chat_id") == telegram_chat_id]
        joined_exam = "\n".join(str(m.get("text") or "") for m in after_exam)
        _must(
            "не удалось сразу доставить кейсы" not in joined_exam.lower(),
            "GAP part1->part2: выявлено системное сообщение о недоставке кейсов",
        )
        je = joined_exam.lower()
        _must(("этап 2" in je) or ("часть 2" in je), "GAP part1->part2: нет сообщения о старте кейсов")
        _must("кейс 1 из" in joined_exam.lower(), "GAP part1->part2: не отправлен текст кейса 1")
        _must(
            "part2-case?token=" not in joined_exam.lower(),
            "part1->part2: в Telegram утекла вспомогательная веб-ссылка вместо чистого текста кейса",
        )
        _must(
            re.search(r"\(\s*[cC]_[^)]+\)", joined_exam) is None,
            "part2 telegram text leaks technical skill identifiers like (C_xxx)",
        )
        print(f"[OK] part1->part2: сообщений сотруднику после экзамена = {len(after_exam)}")

        # Ответы по кейсам -> переход в part3.
        outbound.clear()
        r = client.get("/api/skill-assessment/public/part2-cases?token=" + token)
        _must(r.status_code == 200, f"get public part2 cases failed: {r.status_code} {r.text}")
        payload = r.json()
        answers = {
            "answers": [
                {"case_id": item["case_id"], "answer": f"Решение кейса {idx + 1}: шаги, KPI, риски."}
                for idx, item in enumerate(payload.get("cases") or [])
            ]
        }
        _must(bool(answers["answers"]), "no cases generated for part2")
        r = client.post("/api/skill-assessment/public/part2-cases?token=" + token, json=answers)
        _must(r.status_code == 200, f"submit public part2 cases failed: {r.status_code} {r.text}")

        employee_after_part2 = [m for m in outbound.messages if m.get("chat_id") == telegram_chat_id]
        joined_emp_part2 = "\n".join(str(m.get("text") or "") for m in employee_after_part2)
        _must(
            "оценка по кейсам завершена" in joined_emp_part2.lower(),
            "part2 complete: сотруднику не отправлена сводка оценки кейсов (часть 2)",
        )
        _must(
            "этап 3" in joined_emp_part2.lower(),
            "part2 complete: сотруднику не отправлено уведомление о старте этапа 3",
        )

        r = client.get(f"/api/skill-assessment/sessions/{session_id}")
        _must(r.status_code == 200, f"get assessment after part2 failed: {r.status_code} {r.text}")
        sess = r.json()
        _must(sess.get("phase") == "part3", f"expected phase=part3 after part2, got {sess.get('phase')}")
        manager_token = sess.get("manager_assessment_token")
        _must(bool(manager_token), "manager_assessment_token missing after part2")

        # Проверка 2: после part2 сразу ушло уведомление руководителю.
        manager_msgs = [m for m in outbound.messages if str(m.get("chat_id")) == "mgr_chat_001"]
        joined_mgr = "\n".join(str(m.get("text") or "") for m in manager_msgs)
        _must("этап: оценка руководителем" in joined_mgr.lower(), "GAP part2->part3: нет уведомления руководителю")
        print(f"[OK] part2->part3: сообщений руководителю = {len(manager_msgs)}")

        # Оценка руководителя -> обновлённый протокол сотруднику.
        outbound.clear()
        r = client.get("/api/skill-assessment/public/manager-assessment?token=" + manager_token)
        _must(r.status_code == 200, f"get manager assessment page failed: {r.status_code} {r.text}")
        skills = r.json().get("skills") or []
        _must(bool(skills), "no skills for manager assessment")
        ratings = [{"skill_id": it["skill_id"], "level": 3} for it in skills]
        r = client.post(
            "/api/skill-assessment/public/manager-assessment?token=" + manager_token,
            json={"ratings": ratings},
        )
        _must(r.status_code == 200, f"submit manager assessment failed: {r.status_code} {r.text}")

        employee_msgs = [m for m in outbound.messages if m.get("chat_id") == telegram_chat_id]
        joined_emp = "\n".join(str(m.get("text") or "") for m in employee_msgs)
        _must(
            "оценка руководителя добавлена в общий протокол" in joined_emp.lower(),
            "part3 complete: сотруднику не отправлено обновление протокола",
        )
        print(f"[OK] part3 complete: сообщений сотруднику = {len(employee_msgs)}")

    print("RESULT: PASS (без разрывов между этапами)")


if __name__ == "__main__":
    main()
