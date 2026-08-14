"""
회귀 테스트 — 이미 지나간 스테이지(design)에 채팅으로 새 지시를 보내도
(1) 원본 PRD(pipeline.instruction)가 사라지지 않고 보존되는지,
(2) 아무 반응 없이 조용히 무시되지 않고 안내 메시지가 나가는지,
(3) retry-design으로 실제 재작업을 걸면 design 이후 스테이지가 전부 다시
    돌고 implement 승인이 초기화되는지 확인한다.

실제로 있었던 사고: counter-app에서 "디자인이 사라진것같은데 다시 디자인해줘.
그걸 적용도 하고"라고 채팅을 보냈는데, get_ready_stages가 PENDING 스테이지만
보기 때문에 이미 completed인 design이 재실행되지 않아 아무 반응이 없었다.
게다가 그 채팅 메시지가 pipeline.instruction(원본 PRD 전체)을 통째로
덮어써서, 나중에 어떤 재시도라도 걸렸다면 원본 스펙을 잃어버릴 뻔했다.

실행: cd orchestrator && pytest tests/test_design_retry.py -v
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


async def test_chat_after_completion_preserves_instruction_and_triages_via_pm(monkeypatch):
    original = "PRD: Flutter 카운터 앱. 화면 1개, 버튼 누르면 숫자 1씩 증가."
    p = _completed_project("p1", original)
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main, "project_repos", {})
    monkeypatch.setattr(main, "chat_triage_in_flight", {})
    monkeypatch.setattr(main, "_append_chat_log", lambda *a, **k: None)
    monkeypatch.setattr(main.spawner, "spawn_team", lambda *a, **k: None)

    broadcasts = []

    async def _capture_broadcast(event):
        broadcasts.append(event)

    monkeypatch.setattr(main, "broadcast", _capture_broadcast)

    sent_tasks = []

    async def _fake_send_task(agent_name, pid, task):
        sent_tasks.append((agent_name, pid, task))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main.handle_ws_message(None, {
        "type": "instruction", "project_id": "p1",
        "content": "디자인이 사라진것같은데 다시 디자인해줘. 그걸 적용도 하고",
    })

    # 원본 PRD가 사라지지 않고 보존됐는지
    assert original in p.instruction
    assert "디자인이 사라진것같은데" in p.instruction

    # 아무 반응 없이 무시되지 않고 검토 중이라는 안내가 즉시 나갔는지
    guidance = [e for e in broadcasts if e.get("type") == "agent_message" and e.get("agent") == "system"]
    assert guidance, "이미 지나간 스테이지에 대한 안내 메시지가 없음"
    assert "검토" in guidance[-1]["content"]

    # 사람이 API를 직접 호출하는 대신, PM에게 chat_triage 태스크가 자동으로 나갔는지
    assert len(sent_tasks) == 1
    agent_name, pid, task = sent_tasks[0]
    assert agent_name == "pm"
    assert pid == "p1"
    assert task["stage"] == "chat_triage"
    assert "디자인이 사라진것같은데" in task["instruction"]


async def test_retry_design_resets_downstream_and_requires_reapproval(monkeypatch):
    original = "PRD: Flutter 카운터 앱."
    p = _completed_project("p1", original)
    monkeypatch.setattr(main, "project_repos", {})
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main.spawner, "spawn_team", lambda *a, **k: None)

    sent_tasks = []

    async def _fake_send_task(agent_name, pid, task):
        sent_tasks.append((agent_name, task))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)
    monkeypatch.setattr(main, "broadcast", _noop)

    await main._retry_design_with_feedback(p, "버튼이 사라졌어요")

    assert p.stages["design"].status in (StageStatus.PENDING, StageStatus.RUNNING)
    assert p.stages["implement"].outputs == {}
    assert p.stages["implement"].approved is False
    assert p.stages["qa"].outputs == {}
    assert p.stages["autotest"].outputs == {}

    # 원본 PRD는 보존하고 피드백만 덧붙였는지
    assert original in p.instruction
    assert "버튼이 사라졌어요" in p.instruction

    # design 에이전트(designer/architect)에게 실제로 태스크가 나갔는지
    assert any(name in ("designer", "architect") for name, _ in sent_tasks)
