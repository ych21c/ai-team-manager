"""
회귀 테스트 — 이미 지나간 스테이지에 대한 채팅 후속 요청을 사람이
retry-design/retry-implement API를 직접 호출하지 않아도, PM에게 검토를 맡겨
자동으로 알맞은 단계(design 또는 implement)를 다시 돌리는 chat_triage 라우팅.

핵심 불변식:
  1. scope="design"이면 _retry_design_with_feedback만 불리고 _retry_implement_with_feedback는
     절대 불리지 않는다 (그 반대도 마찬가지).
  2. scope="none"/판단 불가면 아무 파이프라인 상태도 안 바뀌고 reply만 보여준다.
  3. PM이 아직 검토 중일 때 같은 프로젝트에 채팅이 또 오면 chat_triage 태스크를
     중복으로 보내지 않는다 (서로 다른 결정이 경합하는 것을 막기 위함).

실행: cd orchestrator && pytest tests/test_chat_triage.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from workflows.pipeline import Pipeline, StageStatus

pytestmark = pytest.mark.asyncio


async def _noop(*args, **kwargs):
    pass


def _completed_project(pid: str, original_instruction: str) -> Pipeline:
    p = Pipeline(pid, original_instruction)
    p.mark_completed("planning", {})
    p.mark_completed("design", {"design_preview": True})
    p.stages["implement"].approved = True
    p.mark_completed("implement", {"branch": "b1", "pr_number": 1})
    p.mark_completed("qa", {"passed": True})
    p.mark_completed("autotest", {"passed": True})
    p.mark_waiting_approval("release")
    return p


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    monkeypatch.setattr(main, "project_repos", {})
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main, "_add_history", _noop)
    monkeypatch.setattr(main, "chat_triage_in_flight", {})
    # CHATLOG_DIR은 컨테이너 안의 /workspace 마운트를 전제로 하는 순수 디버깅용
    # 부가 기능이라, 라우팅 로직 테스트에서는 디스크에 실제로 안 쓰게 꺼둔다.
    monkeypatch.setattr(main, "_append_chat_log", lambda *a, **k: None)


# ── _dispatch_chat_triage ─────────────────────────────────────────────

async def test_dispatch_sends_chat_triage_task_to_pm_scoped_queue(monkeypatch):
    p = _completed_project("p1", "PRD 원본")
    sent_tasks = []

    async def _fake_send_task(agent_name, pid, task):
        sent_tasks.append((agent_name, pid, task))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main._dispatch_chat_triage(p, "디자인이 사라진 것 같아요")

    assert len(sent_tasks) == 1
    agent_name, pid, task = sent_tasks[0]
    # pm은 GLOBAL_SHARED_AGENTS가 아니라 프로젝트 전용 큐를 쓴다 — project_id가 None이면 안 됨.
    assert agent_name == "pm"
    assert pid == "p1"
    assert task["stage"] == "chat_triage"
    assert task["instruction"] == "디자인이 사라진 것 같아요"
    assert "planning" in task["context"] and "design" in task["context"]
    assert task["context"]["stage_status"]["implement"] == StageStatus.COMPLETED.value
    assert "p1" in main.chat_triage_in_flight


# ── _handle_chat_triage_result ─────────────────────────────────────────

async def test_triage_scope_design_calls_retry_design_not_implement(monkeypatch):
    p = _completed_project("p1", "PRD 원본")
    design_calls = []
    implement_calls = []

    async def _fake_retry_design(pipeline, feedback, scenario_key=None):
        design_calls.append(feedback)

    async def _fake_retry_implement(pipeline, feedback, scenario_key=None):
        implement_calls.append(feedback)
        raise AssertionError("implement 재작업이 잘못 호출됨")

    monkeypatch.setattr(main, "_retry_design_with_feedback", _fake_retry_design)
    monkeypatch.setattr(main, "_retry_implement_with_feedback", _fake_retry_implement)
    monkeypatch.setattr(main, "broadcast", _noop)
    main.chat_triage_in_flight["p1"] = 0.0

    await main._handle_chat_triage_result(p, "p1", {
        "scope": "design", "feedback": "버튼이 사라짐", "reply": "디자인부터 다시 만들게요",
    })

    assert design_calls == ["버튼이 사라짐"]
    assert implement_calls == []
    assert "p1" not in main.chat_triage_in_flight


async def test_triage_scope_implement_calls_retry_implement_not_design(monkeypatch):
    p = _completed_project("p1", "PRD 원본")
    design_calls = []
    implement_calls = []

    async def _fake_retry_design(pipeline, feedback, scenario_key=None):
        design_calls.append(feedback)
        raise AssertionError("design 재작업이 잘못 호출됨")

    async def _fake_retry_implement(pipeline, feedback, scenario_key=None):
        implement_calls.append(feedback)

    monkeypatch.setattr(main, "_retry_design_with_feedback", _fake_retry_design)
    monkeypatch.setattr(main, "_retry_implement_with_feedback", _fake_retry_implement)
    monkeypatch.setattr(main, "broadcast", _noop)

    await main._handle_chat_triage_result(p, "p1", {
        "scope": "implement", "feedback": "로그인 버튼 클릭 시 크래시", "reply": "구현만 다시 확인할게요",
    })

    assert implement_calls == ["로그인 버튼 클릭 시 크래시"]
    assert design_calls == []


async def test_triage_scope_none_only_broadcasts_reply_no_pipeline_change(monkeypatch):
    p = _completed_project("p1", "PRD 원본")
    design_status_before = p.stages["design"].status
    implement_status_before = p.stages["implement"].status

    async def _fail_if_called(*a, **k):
        raise AssertionError("scope=none인데 재작업 함수가 호출됨")

    monkeypatch.setattr(main, "_retry_design_with_feedback", _fail_if_called)
    monkeypatch.setattr(main, "_retry_implement_with_feedback", _fail_if_called)

    broadcasts = []

    async def _capture(event):
        broadcasts.append(event)

    monkeypatch.setattr(main, "broadcast", _capture)

    await main._handle_chat_triage_result(p, "p1", {
        "scope": "none", "feedback": "", "reply": "화면 문제인지 동작 문제인지 알려주세요",
    })

    assert any("화면 문제인지 동작 문제인지" in e["content"] for e in broadcasts)
    assert p.stages["design"].status == design_status_before
    assert p.stages["implement"].status == implement_status_before


async def test_triage_malformed_scope_falls_back_to_none(monkeypatch):
    p = _completed_project("p1", "PRD 원본")

    async def _fail_if_called(*a, **k):
        raise AssertionError("잘못된 scope인데 재작업 함수가 호출됨")

    monkeypatch.setattr(main, "_retry_design_with_feedback", _fail_if_called)
    monkeypatch.setattr(main, "_retry_implement_with_feedback", _fail_if_called)
    monkeypatch.setattr(main, "broadcast", _noop)

    # outputs가 비어있거나(에이전트 실패) scope가 이상한 값이어도 안전하게 처리돼야 함
    await main._handle_chat_triage_result(p, "p1", {})
    await main._handle_chat_triage_result(p, "p1", {"scope": "release"})


# ── in-flight guard (handle_ws_message) ─────────────────────────────────

async def test_in_flight_guard_skips_second_dispatch_while_pending(monkeypatch):
    p = _completed_project("p1", "PRD 원본")
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main.spawner, "spawn_team", lambda *a, **k: None)
    monkeypatch.setattr(main, "broadcast", _noop)

    sent_tasks = []

    async def _fake_send_task(agent_name, pid, task):
        sent_tasks.append((agent_name, pid, task))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main.handle_ws_message(None, {
        "type": "instruction", "project_id": "p1", "content": "디자인이 이상해요",
    })
    assert len(sent_tasks) == 1  # 첫 메시지 → triage 태스크 1건 발송

    await main.handle_ws_message(None, {
        "type": "instruction", "project_id": "p1", "content": "그리고 이것도 고쳐주세요",
    })
    assert len(sent_tasks) == 1  # 검토가 진행 중이므로 두 번째 메시지는 새 태스크를 안 보냄

    # 하지만 두 번째 메시지도 instruction에는 반영돼야 함 (기존 append 동작 유지)
    assert "그리고 이것도 고쳐주세요" in p.instruction


async def test_stale_in_flight_flag_allows_new_dispatch(monkeypatch):
    p = _completed_project("p1", "PRD 원본")
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main.spawner, "spawn_team", lambda *a, **k: None)
    monkeypatch.setattr(main, "broadcast", _noop)

    sent_tasks = []

    async def _fake_send_task(agent_name, pid, task):
        sent_tasks.append((agent_name, pid, task))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)
    # 에이전트가 응답 없이 죽은 것처럼 timeout보다 오래된 in-flight 기록을 만든다
    # (0.0처럼 falsy한 값을 쓰면 "in_flight_ts and ..." 분기 자체가 안 타서 이 테스트의
    # 의도— stale timeout 판정 —를 검증하지 못하므로, 실제로 지난 과거 시각을 넣는다).
    main.chat_triage_in_flight["p1"] = main.time.time() - main.CHAT_TRIAGE_TIMEOUT_SEC - 1

    await main.handle_ws_message(None, {
        "type": "instruction", "project_id": "p1", "content": "디자인이 이상해요",
    })

    assert len(sent_tasks) == 1  # stale 플래그는 무시하고 새로 보냄
