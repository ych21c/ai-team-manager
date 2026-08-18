"""
회귀 테스트 — design 스테이지는 designer+architect 둘이 나눠 맡는데, 먼저
끝난 에이전트 하나만으로 스테이지 전체가 끝난 것처럼 다음 게이트(구현 시작
승인)가 열려버리던 레이스를 막는지 확인한다.

배경: handle_agent_event가 pipeline.mark_completed를 agent_name 없이 호출해서,
어느 쪽이 먼저 끝나든 그 하나만으로 design이 즉시 COMPLETED로 전이하고
advance_pipeline이 바로 다음 게이트를 열어버렸다. architect가 designer보다
먼저 끝나면(architect 산출물이 보통 더 짧아 실제로 있을 법한 순서), 실제
목업이 하나도 안 나온 상태에서 "디자인 목업을 확인하고 승인해주세요"가 뜰 수
있었다. 지금은 handle_agent_event가 agent_name을 넘겨서, stage.agents 전원이
보고해야만 다음 게이트가 열린다(orchestrator/workflows/pipeline.py의
mark_completed).

실행: cd orchestrator && pytest tests/test_design_parallel_agents_gate.py -v
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


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    monkeypatch.setattr(main, "broadcast", _noop)
    monkeypatch.setattr(main, "_add_history", _noop)
    monkeypatch.setattr(main, "project_repos", {})
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main, "project_docs", {})
    monkeypatch.setattr(main.spawner, "spawn_team", lambda *a, **k: None)
    monkeypatch.setattr(main.redis, "send_task", _noop)


def _project_at_design(pid="p1") -> Pipeline:
    p = Pipeline(pid, "PRD")
    p.mark_completed("planning", {})
    p.mark_running("design")
    return p


async def test_first_agent_alone_does_not_complete_design_stage(monkeypatch):
    p = _project_at_design()
    monkeypatch.setattr(main, "projects", {"p1": p})

    await main.handle_agent_event({
        "project_id": "p1", "agent": "architect", "type": "stage_completed",
        "stage": "design", "outputs": {"agent": "architect", "architecture_summary": "..."},
    })

    assert p.stages["design"].status == StageStatus.RUNNING
    assert p.stages["design"].agents_done == ["architect"]
    # implement 게이트가 아직 열리면 안 된다 — architect만 끝났을 뿐 designer는 아직.
    assert p.stages["implement"].status == StageStatus.PENDING


async def test_second_agent_completes_design_and_opens_next_gate(monkeypatch):
    p = _project_at_design()
    monkeypatch.setattr(main, "projects", {"p1": p})

    await main.handle_agent_event({
        "project_id": "p1", "agent": "architect", "type": "stage_completed",
        "stage": "design", "outputs": {"agent": "architect", "architecture_summary": "..."},
    })
    await main.handle_agent_event({
        "project_id": "p1", "agent": "designer", "type": "stage_completed",
        "stage": "design", "outputs": {"agent": "designer", "summary": "...", "design_preview": True},
    })

    assert p.stages["design"].status == StageStatus.COMPLETED
    assert p.stages["implement"].status == StageStatus.WAITING


async def test_outputs_from_both_agents_are_preserved_not_overwritten(monkeypatch):
    """architect가 architecture_summary에, designer가 summary에 각자 쓰니
    누가 먼저/나중에 끝나든 서로의 산출물을 지우면 안 된다."""
    p = _project_at_design()
    monkeypatch.setattr(main, "projects", {"p1": p})

    await main.handle_agent_event({
        "project_id": "p1", "agent": "architect", "type": "stage_completed",
        "stage": "design", "outputs": {"agent": "architect", "architecture_summary": "architect의 구조 설계"},
    })
    await main.handle_agent_event({
        "project_id": "p1", "agent": "designer", "type": "stage_completed",
        "stage": "design", "outputs": {"agent": "designer", "summary": "designer의 목업 요약"},
    })

    outputs = p.stages["design"].outputs
    assert outputs["summary"] == "designer의 목업 요약"
    assert outputs["architecture_summary"] == "architect의 구조 설계"


async def test_architecture_doc_reads_architecture_summary_key(monkeypatch):
    """_doc(project_id)["architecture"]가 architect의 architecture_summary를
    읽는지 — 예전엔 공유 "summary" 키를 읽어서, designer가 나중에 끝나면
    architect의 기술 스펙 대신 designer의 목업 요약이 들어가곤 했다."""
    p = _project_at_design()
    monkeypatch.setattr(main, "projects", {"p1": p})
    docs = {}
    monkeypatch.setattr(main, "project_docs", docs)

    await main.handle_agent_event({
        "project_id": "p1", "agent": "architect", "type": "stage_completed",
        "stage": "design", "outputs": {"agent": "architect", "architecture_summary": "architect의 구조 설계"},
    })

    assert docs["p1"]["architecture"] == "architect의 구조 설계"
