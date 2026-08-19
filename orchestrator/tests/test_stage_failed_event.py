"""
회귀 테스트 — agent가 process_task 도중 예외로 죽었을 때 보내는 stage_failed
이벤트를 orchestrator가 처리하는 부분(orchestrator/main.py의 handle_agent_event).

예전엔 agents/base/agent.py의 main() 루프가 process_task 예외를 로컬 stderr에만
찍고 끝나서, orchestrator는 그 스테이지가 실패한 줄 전혀 몰랐다. recoveryfit
프로젝트에서 Anthropic API 크레딧 소진 에러 이후 design 스테이지가 20시간 넘게
"running"에 멈춰있었고, 사람이 컨테이너 로그를 직접 뒤져서 docker restart로
수동 복구해야 했다(실제로 재현됨). agent가 stage_failed를 명시적으로 보고하면
바로 FAILED로 반영해서 화면에서 즉시 알 수 있고 재실행/폐기할 수 있게 한다.

실행: cd orchestrator && pytest tests/test_stage_failed_event.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from workflows.pipeline import Pipeline, StageStatus


async def _noop(*args, **kwargs):
    pass


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    monkeypatch.setattr(main, "broadcast", _noop)
    monkeypatch.setattr(main, "_add_history", _noop)
    monkeypatch.setattr(main, "project_repos", {})
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main, "project_docs", {})


@pytest.mark.asyncio
async def test_stage_failed_marks_stage_failed(monkeypatch):
    p = Pipeline("p1", "PRD 원본")
    p.mark_completed("planning", {})
    p.stages["design"].approved = True
    p.mark_running("design")
    monkeypatch.setattr(main, "projects", {"p1": p})

    await main.handle_agent_event({
        "type": "stage_failed", "project_id": "p1", "agent": "designer",
        "stage": "design", "error": "credit balance too low",
    })

    assert p.stages["design"].status == StageStatus.FAILED
    assert p.stages["design"].outputs["error"] == "credit balance too low"
    assert p.stages["design"].outputs["agent"] == "designer"


@pytest.mark.asyncio
async def test_stage_failed_broadcasts_stage_update_and_chat_message(monkeypatch):
    p = Pipeline("p1", "PRD 원본")
    p.mark_completed("planning", {})
    p.stages["design"].approved = True
    p.mark_running("design")
    monkeypatch.setattr(main, "projects", {"p1": p})
    sent = []
    monkeypatch.setattr(main, "broadcast", lambda e: sent.append(e) or _noop())

    await main.handle_agent_event({
        "type": "stage_failed", "project_id": "p1", "agent": "architect",
        "stage": "design", "error": "boom",
    })

    types = [e["type"] for e in sent]
    assert "stage_update" in types
    assert "agent_message" in types
    assert "project_updated" in types
    stage_update = next(e for e in sent if e["type"] == "stage_update")
    assert stage_update["status"] == "failed"
    assert stage_update["stage"] == "design"


@pytest.mark.asyncio
async def test_stage_failed_unknown_stage_name_is_ignored_not_crash(monkeypatch):
    """chat_triage는 Pipeline.stages에 없는 가짜 스테이지라 agent.py가 애초에
    stage_failed를 안 보내지만(호출부에서 걸러짐), 방어적으로 여기서도 모르는
    stage_name이 오면 KeyError로 죽지 않고 조용히 무시해야 한다."""
    p = Pipeline("p1", "PRD 원본")
    monkeypatch.setattr(main, "projects", {"p1": p})

    await main.handle_agent_event({
        "type": "stage_failed", "project_id": "p1", "agent": "pm",
        "stage": "chat_triage", "error": "boom",
    })
    # 예외 없이 여기까지 오면 통과


@pytest.mark.asyncio
async def test_stage_failed_unknown_project_is_ignored():
    """존재하지 않는 project_id면 handle_agent_event 맨 앞의 공용 가드에서
    바로 return한다 — 다른 이벤트 타입과 동일한 동작."""
    await main.handle_agent_event({
        "type": "stage_failed", "project_id": "ghost", "agent": "pm",
        "stage": "design", "error": "boom",
    })
