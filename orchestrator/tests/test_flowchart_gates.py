"""
회귀 테스트 — 플로우차트 탭(스프린트 게이트 UI)이 새로 쓰는 엔드포인트/헬퍼들.

  1. _reset_stage_cascade — "폐기" 버튼의 핵심. 대상 스테이지부터 그 이후만
     PENDING/빈 outputs/미승인으로 되돌리고, 그 이전 스테이지는 절대 안 건드리는지.
  2. discard_stage 엔드포인트 — advance_pipeline을 호출하지 않는지(= 폐기 후
     자동으로 다음 태스크가 큐에 들어가면 안 됨). planning은 폐기 불가인지.
  3. 신규 retry 헬퍼(qa/autotest/release) — 각각 올바른 스테이지 집합만
     리셋하고, GLOBAL_SHARED_AGENTS 여부에 따라 큐 라우팅(project_id vs None)이
     맞는지.
  4. approve_stage에 extra_input을 주면(디자이너 "Go" 버튼) instruction에
     반영되는지.

실행: cd orchestrator && pytest tests/test_flowchart_gates.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from workflows.pipeline import Pipeline, StageStatus


async def _noop(*args, **kwargs):
    pass


def _completed_project(pid: str = "p1", instruction: str = "PRD 원본") -> Pipeline:
    p = Pipeline(pid, instruction)
    p.mark_completed("planning", {"summary": "기획 완료"})
    p.stages["design"].approved = True
    p.mark_completed("design", {"summary": "디자인 완료"})
    p.stages["implement"].approved = True
    p.mark_completed("implement", {"branch": "b1", "pr_number": 1})
    p.mark_completed("qa", {"passed": True})
    p.stages["autotest"].approved = True
    p.mark_completed("autotest", {"passed": True})
    return p


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    monkeypatch.setattr(main, "broadcast", _noop)
    monkeypatch.setattr(main, "project_repos", {})
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main, "project_docs", {})
    monkeypatch.setattr(main.spawner, "spawn_team", lambda *a, **k: None)


# ── _reset_stage_cascade ────────────────────────────────────────────────

def test_reset_stage_cascade_resets_target_and_downstream_only():
    p = _completed_project()
    main._reset_stage_cascade(p, "qa")

    assert p.stages["planning"].status == StageStatus.COMPLETED
    assert p.stages["design"].status == StageStatus.COMPLETED
    assert p.stages["implement"].status == StageStatus.COMPLETED
    for name in ("qa", "autotest", "release"):
        assert p.stages[name].status == StageStatus.PENDING
        assert p.stages[name].outputs == {}
        assert p.stages[name].approved is False


def test_reset_stage_cascade_from_design_clears_implement_approval():
    p = _completed_project()
    main._reset_stage_cascade(p, "design")

    assert p.stages["planning"].status == StageStatus.COMPLETED
    assert p.stages["design"].status == StageStatus.PENDING
    assert p.stages["implement"].status == StageStatus.PENDING
    assert p.stages["implement"].approved is False
    assert p.stages["release"].status == StageStatus.PENDING


# ── discard_stage 엔드포인트 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discard_stage_does_not_call_advance_pipeline(monkeypatch):
    p = _completed_project()
    monkeypatch.setattr(main, "projects", {"p1": p})
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append(agent_name)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    result = await main.discard_stage("p1", "qa")

    assert result == {"ok": True}
    assert sent == []  # advance_pipeline이 호출됐다면 여기 뭔가 들어왔을 것
    assert p.stages["qa"].status == StageStatus.PENDING
    assert p.stages["implement"].status == StageStatus.COMPLETED  # 이전 단계는 그대로


@pytest.mark.asyncio
async def test_discard_planning_is_rejected(monkeypatch):
    p = _completed_project()
    monkeypatch.setattr(main, "projects", {"p1": p})

    with pytest.raises(main.HTTPException) as exc_info:
        await main.discard_stage("p1", "planning")

    assert exc_info.value.status_code == 400
    assert p.stages["planning"].status == StageStatus.COMPLETED  # 건드려지지 않았어야 함


# ── 신규 retry 헬퍼 ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_qa_resets_qa_and_autotest_only(monkeypatch):
    p = _completed_project()
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append((agent_name, pid))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main._retry_qa_with_feedback(p, "다시 확인해줘")

    assert p.stages["qa"].status == StageStatus.RUNNING
    assert p.stages["autotest"].status == StageStatus.PENDING
    assert p.stages["autotest"].approved is False
    assert p.stages["implement"].status == StageStatus.COMPLETED  # implement는 안 건드림
    # qa/autotest는 GLOBAL_SHARED_AGENTS라 큐가 project_id로 안 나뉘어야 함(None)
    assert sent == [("qa", None)]


@pytest.mark.asyncio
async def test_retry_autotest_resets_only_autotest(monkeypatch):
    p = _completed_project()
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append((agent_name, pid))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main._retry_autotest_with_feedback(p, "")

    assert p.stages["autotest"].status == StageStatus.RUNNING
    assert p.stages["qa"].status == StageStatus.COMPLETED  # qa는 안 건드림
    assert sent == [("autotest", None)]


@pytest.mark.asyncio
async def test_retry_release_uses_project_scoped_queue(monkeypatch):
    p = _completed_project()
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append((agent_name, pid))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main._retry_release_with_feedback(p, "다시")

    assert p.stages["release"].status == StageStatus.RUNNING
    # release는 TeamSpawner가 프로젝트별로 격리하는 컨테이너라 project_id로 큐가 분리돼야 함
    assert sent == [("release", "p1")]


# ── approve_stage: extra_input ("Go" 버튼) ───────────────────────────────

@pytest.mark.asyncio
async def test_approve_with_extra_input_appends_to_instruction(monkeypatch):
    p = _completed_project()
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main.redis, "send_task", _noop)
    p.mark_waiting_approval("release")

    await main.approve_stage("p1", "release", main.GateApprove(extra_input="배포 전에 스크린샷 첨부해줘"))

    assert "배포 전에 스크린샷 첨부해줘" in p.instruction
    assert p.stages["release"].approved is True


@pytest.mark.asyncio
async def test_approve_without_extra_input_leaves_instruction_untouched(monkeypatch):
    p = _completed_project()
    original = p.instruction
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main.redis, "send_task", _noop)
    p.mark_waiting_approval("release")

    await main.approve_stage("p1", "release")

    assert p.instruction == original
