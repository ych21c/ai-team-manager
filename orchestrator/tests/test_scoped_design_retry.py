"""
회귀 테스트 — 채팅으로 "1번 화면 로고가 이상해서 다시 디자인해줘"처럼 특정
화면 하나를 겨냥한 재작업 요청을 보내도, design 스테이지 전체(이 프로젝트의
모든 Jira 스토리/시나리오)가 통째로 재생성되던 사고.

실제로 있었던 사고: recoveryFit에서 화면 1개 관련 피드백을 보냈는데 시나리오
8개(ATM-5~ATM-12)가 전부 다시 생성됐다. 원인은 advance_pipeline이 design
스테이지를 돌릴 때마다 무조건 이 프로젝트의 Jira 스토리 전체를 시나리오
목록으로 넘겼기 때문 — publish_design 자체는 이미 받은 키만 건드리도록
돼 있었으므로, retry 쪽에서 "이번엔 이 키 하나만" 넘길 수 있게 좁혔다.

실행: cd orchestrator && pytest tests/test_scoped_design_retry.py -v
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


def _completed_project_with_scenarios(pid: str, original_instruction: str) -> Pipeline:
    p = Pipeline(pid, original_instruction)
    p.mark_completed("planning", {})
    p.mark_completed("design", {"design_preview": True, "summary": "8개 화면 디자인 완료"})
    p.stages["implement"].approved = True
    p.mark_completed("implement", {"branch": "b1", "pr_number": 1})
    p.mark_completed("qa", {"passed": True})
    p.mark_completed("autotest", {"passed": True})
    p.mark_waiting_approval("release")
    return p


def _story_titles() -> dict:
    return {f"ATM-{n}": f"화면 {n}" for n in range(5, 13)}  # ATM-5 ~ ATM-12, 8개


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    monkeypatch.setattr(main, "project_repos", {})
    monkeypatch.setattr(main, "add_jira_comment", _noop)
    monkeypatch.setattr(main, "broadcast", _noop)
    monkeypatch.setattr(main.spawner, "spawn_team", lambda *a, **k: None)


async def test_scoped_retry_preserves_outputs_and_tags_instruction(monkeypatch):
    p = _completed_project_with_scenarios("p1", "PRD 원본")
    monkeypatch.setattr(main, "project_jira", {"p1": {"stories": list(_story_titles()), "story_titles": _story_titles()}})
    monkeypatch.setattr(main.redis, "send_task", _noop)

    await main._retry_design_with_feedback(p, "로고가 이상함", "ATM-5")

    # 스코프 지정 시엔 design.outputs를 통째로 비우지 않는다 — 다른 시나리오들의
    # 요약(예: "8개 화면 디자인 완료")을 잃으면 안 되므로.
    assert p.stages["design"].outputs.get("summary") == "8개 화면 디자인 완료"
    assert "[디자인 재작업 요청 - ATM-5] 로고가 이상함" in p.instruction
    # implement/qa/autotest는 여전히 전체 리셋(안전을 위해 유지하기로 한 트레이드오프)
    assert p.stages["implement"].outputs == {}
    assert p.stages["implement"].approved is False
    assert p.stages["qa"].outputs == {}
    assert p.stages["autotest"].outputs == {}


async def test_scoped_retry_only_sends_target_scenario_to_designer(monkeypatch):
    p = _completed_project_with_scenarios("p1", "PRD 원본")
    monkeypatch.setattr(main, "project_jira", {"p1": {"stories": list(_story_titles()), "story_titles": _story_titles()}})

    sent_tasks = []

    async def _fake_send_task(agent_name, pid, task):
        sent_tasks.append((agent_name, task))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main._retry_design_with_feedback(p, "로고가 이상함", "ATM-5")

    designer_tasks = [t for name, t in sent_tasks if name == "designer"]
    assert len(designer_tasks) == 1
    assert designer_tasks[0]["context"]["scenarios"] == [{"key": "ATM-5", "title": "화면 5"}]


async def test_scenario_scope_is_a_one_shot_hint_cleared_after_dispatch(monkeypatch):
    p = _completed_project_with_scenarios("p1", "PRD 원본")
    monkeypatch.setattr(main, "project_jira", {"p1": {"stories": list(_story_titles()), "story_titles": _story_titles()}})
    monkeypatch.setattr(main.redis, "send_task", _noop)

    await main._retry_design_with_feedback(p, "로고가 이상함", "ATM-5")

    assert p.stages["design"].scenario_scope is None


async def test_rerun_stage_endpoint_threads_scenario_key_through(monkeypatch):
    """실제 사고 경로 재현: 플로우차트 탭 "Run" 버튼(POST .../stage/design/rerun)이
    scenario_key를 받아 _retry_design_with_feedback으로 그대로 넘기는지 확인한다.
    예전엔 StageRerun에 scenario_key 필드 자체가 없어서 이 경로로 재실행하면
    항상 scenario_key=None → 시나리오 전체 재작업이었다(recoveryfit ATM-5~12
    전체 재생성 사고, 이번엔 채팅 트리아지가 아니라 이 버튼을 통해 재현됨)."""
    p = _completed_project_with_scenarios("p1", "PRD 원본")
    main.projects["p1"] = p
    monkeypatch.setattr(main, "project_jira", {"p1": {"stories": list(_story_titles()), "story_titles": _story_titles()}})

    sent_tasks = []

    async def _fake_send_task(agent_name, pid, task):
        sent_tasks.append((agent_name, task))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main.rerun_stage("p1", "design", main.StageRerun(feedback="화면 5만 다시", scenario_key="ATM-5"))

    designer_tasks = [t for name, t in sent_tasks if name == "designer"]
    assert len(designer_tasks) == 1
    assert designer_tasks[0]["context"]["scenarios"] == [{"key": "ATM-5", "title": "화면 5"}]
    assert "[디자인 재작업 요청 - ATM-5]" in p.instruction


async def test_rerun_stage_endpoint_without_scenario_key_still_works(monkeypatch):
    """scenario_key를 안 주면(기존 동작) 여전히 전체 재작업으로 폴백해야 한다 —
    하위 호환 확인."""
    p = _completed_project_with_scenarios("p1", "PRD 원본")
    main.projects["p1"] = p
    monkeypatch.setattr(main, "project_jira", {"p1": {"stories": list(_story_titles()), "story_titles": _story_titles()}})

    sent_tasks = []

    async def _fake_send_task(agent_name, pid, task):
        sent_tasks.append((agent_name, task))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main.rerun_stage("p1", "design", main.StageRerun(feedback=""))

    designer_tasks = [t for name, t in sent_tasks if name == "designer"]
    assert len(designer_tasks) == 1
    assert len(designer_tasks[0]["context"]["scenarios"]) == 8


async def test_unknown_scenario_key_falls_back_to_full_retry(monkeypatch):
    p = _completed_project_with_scenarios("p1", "PRD 원본")
    monkeypatch.setattr(main, "project_jira", {"p1": {"stories": list(_story_titles()), "story_titles": _story_titles()}})

    sent_tasks = []

    async def _fake_send_task(agent_name, pid, task):
        sent_tasks.append((agent_name, task))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main._retry_design_with_feedback(p, "로고가 이상함", "ATM-999")

    # 존재하지 않는 키는 scenario_key=None과 동일하게 폴백 — 전체 재작업
    assert p.stages["design"].outputs == {}
    assert "[디자인 재작업 요청]" in p.instruction
    assert "[디자인 재작업 요청 - ATM-999]" not in p.instruction
    designer_tasks = [t for name, t in sent_tasks if name == "designer"]
    assert len(designer_tasks) == 1
    assert len(designer_tasks[0]["context"]["scenarios"]) == 8
