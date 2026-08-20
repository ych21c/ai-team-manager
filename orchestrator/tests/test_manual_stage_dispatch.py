"""
회귀 테스트 — MANUAL_STAGES(implement/qa를 컨테이너 에이전트 대신 사람이 처리하게
하는 비용 절감 우회)가 두 지점에서 제대로 동작하는지.

  1. advance_pipeline의 태스크 발송 루프 — MANUAL_STAGES에 있는 스테이지는
     redis.send_task를 호출하지 않고 MANUAL_TASKS_DIR에 태스크 파일만 써야 한다.
     MANUAL_STAGES에 없는 스테이지는 지금까지처럼 큐로 나가야 한다(회귀 방지).
  2. manual-result 엔드포인트 — handle_agent_event와 동일한 경로를 타서(성공 시
     mark_completed, 실패+needs_rework 시 _route_needs_rework_or_fail) 기존 완료
     처리 로직을 그대로 재사용하는지.

실행: cd orchestrator && pytest tests/test_manual_stage_dispatch.py -v
"""
import json
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
    monkeypatch.setattr(main, "project_repos", {"p1": "me/repo"})
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main, "project_docs", {})
    monkeypatch.setattr(main.spawner, "spawn_team", lambda *a, **k: None)
    monkeypatch.setattr(main, "MANUAL_STAGES", set())  # 기본은 꺼짐 — 각 테스트가 필요한 만큼만 켬


def _project_ready_for_qa(pid: str = "p1") -> Pipeline:
    p = Pipeline(pid, "PRD 원본")
    p.mark_completed("planning", {})
    p.stages["design"].approved = True
    p.mark_completed("design", {})
    p.stages["implement"].approved = True
    p.mark_completed("implement", {"branch": "b1", "pr_number": 1})
    return p


# ── advance_pipeline 디스패치 분기 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_manual_stage_writes_task_file_instead_of_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MANUAL_STAGES", {"qa"})
    monkeypatch.setattr(main, "MANUAL_TASKS_DIR", str(tmp_path))
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append(agent_name)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = _project_ready_for_qa()
    await main.advance_pipeline(p)

    assert sent == []  # qa는 큐로 나가면 안 됨
    assert p.stages["qa"].status == StageStatus.RUNNING

    files = list(tmp_path.glob("p1_qa_qa_*.json"))
    assert len(files) == 1
    task = json.loads(files[0].read_text())
    assert task["project_id"] == "p1"
    assert task["stage"] == "qa"
    assert task["github_repo"] == "me/repo"


@pytest.mark.asyncio
async def test_non_manual_stage_still_uses_queue(monkeypatch, tmp_path):
    """MANUAL_STAGES에 qa만 있으면 다른 스테이지는 지금까지처럼 큐로 나가야 한다."""
    monkeypatch.setattr(main, "MANUAL_STAGES", {"qa"})
    monkeypatch.setattr(main, "MANUAL_TASKS_DIR", str(tmp_path))
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append(agent_name)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = Pipeline("p1", "PRD 원본")
    await main.advance_pipeline(p)  # planning만 ready

    assert sent == ["pm"]
    assert list(tmp_path.glob("*.json")) == []


# ── manual-result 엔드포인트 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_manual_result_completes_stage_like_a_real_agent_would(monkeypatch):
    p = _project_ready_for_qa()
    p.mark_running("qa")
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main, "advance_pipeline", _noop)  # 다음 스테이지 디스패치는 범위 밖

    result = await main.manual_stage_result(
        "p1", "qa", main.ManualStageResult(agent="qa", outputs={"passed": True, "summary": "통과"}),
    )

    assert result == {"ok": True}
    assert p.stages["qa"].status == StageStatus.COMPLETED
    assert p.stages["qa"].outputs == {"passed": True, "summary": "통과"}


@pytest.mark.asyncio
async def test_manual_result_needs_rework_routes_to_implement_retry(monkeypatch):
    """QA를 사람이 대신 처리했어도 실패+needs_rework면 기존 라우팅
    (_route_needs_rework_or_fail → qa_retry_counts 증가 → implement 재작업 요청)을
    그대로 타야 한다 — manual-result 전용 분기를 새로 만들지 않았는지 확인."""
    p = _project_ready_for_qa()
    p.mark_running("qa")
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main, "qa_retry_counts", {})
    monkeypatch.setattr(main, "_add_history", _noop)  # 실제 Confluence 동기화 호출 방지

    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append(agent_name)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main.manual_stage_result(
        "p1", "qa",
        main.ManualStageResult(agent="qa", outputs={"passed": False, "needs_rework": True, "feedback": "재현됨"}),
    )

    assert main.qa_retry_counts["p1"] == 1
    assert p.stages["qa"].status == StageStatus.PENDING  # implement 재작업이 끝나야 다시 돎
    assert p.stages["implement"].status == StageStatus.RUNNING
    assert sent == ["implement"]


@pytest.mark.asyncio
async def test_manual_result_unknown_project_404s():
    monkeypatch_projects_backup = dict(main.projects)
    main.projects.clear()
    try:
        with pytest.raises(main.HTTPException) as exc_info:
            await main.manual_stage_result("nope", "qa", main.ManualStageResult(agent="qa", outputs={}))
        assert exc_info.value.status_code == 404
    finally:
        main.projects.update(monkeypatch_projects_backup)
