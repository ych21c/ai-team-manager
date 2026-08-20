"""
회귀 테스트 — project_manual_implement(implement 스테이지만 컨테이너 에이전트 대신
사람이 처리하게 하는, 프로젝트별 런타임 토글) 관련 동작.

  1. set_manual_implement 엔드포인트 — 토글이 project_manual_implement에 반영되고
     _make_project_info/저장 스냅샷에 노출되는지.
  2. advance_pipeline/_retry_implement_with_feedback의 태스크 발송 — 토글이 켜진
     프로젝트의 implement만 큐 대신 MANUAL_TASKS_DIR에 파일로 나가고, QA는 토글과
     무관하게 항상 큐로 나가야 한다(요구사항: "qa validation은 그대로 가야지").
  3. manual-result 엔드포인트 — handle_agent_event와 동일한 경로를 타서(성공 시
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
    monkeypatch.setattr(main, "_add_history", _noop)  # 실제 Confluence 동기화 호출 방지
    monkeypatch.setattr(main, "_save_project", lambda pid: None)  # 디스크 I/O는 이 테스트 범위 밖
    monkeypatch.setattr(main.spawner, "spawn_team", lambda *a, **k: None)
    monkeypatch.setattr(main, "project_manual_implement", {})  # 기본은 꺼짐 — 각 테스트가 필요한 만큼만 켬


def _project_ready_for_qa(pid: str = "p1") -> Pipeline:
    p = Pipeline(pid, "PRD 원본")
    p.mark_completed("planning", {})
    p.stages["design"].approved = True
    p.mark_completed("design", {})
    p.stages["implement"].approved = True
    p.mark_completed("implement", {"branch": "b1", "pr_number": 1})
    return p


# ── set_manual_implement 엔드포인트 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_set_manual_implement_toggles_and_exposes_in_project_info(monkeypatch):
    p = Pipeline("p1", "PRD 원본")
    monkeypatch.setattr(main, "projects", {"p1": p})

    result = await main.set_manual_implement("p1", main.ManualImplementToggle(enabled=True))

    assert result == {"ok": True, "manual_implement": True}
    assert main.project_manual_implement["p1"] is True
    assert main._make_project_info("p1")["manual_implement"] is True

    await main.set_manual_implement("p1", main.ManualImplementToggle(enabled=False))
    assert main.project_manual_implement["p1"] is False
    assert main._make_project_info("p1")["manual_implement"] is False


@pytest.mark.asyncio
async def test_set_manual_implement_unknown_project_404s():
    with pytest.raises(main.HTTPException) as exc_info:
        await main.set_manual_implement("nope", main.ManualImplementToggle(enabled=True))
    assert exc_info.value.status_code == 404


# ── 디스패치 분기: implement만 토글 대상, qa는 항상 큐로 ───────────────────

@pytest.mark.asyncio
async def test_manual_implement_writes_task_file_instead_of_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "project_manual_implement", {"p1": True})
    monkeypatch.setattr(main, "MANUAL_TASKS_DIR", str(tmp_path))
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append(agent_name)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = Pipeline("p1", "PRD 원본")
    p.mark_completed("planning", {})
    p.stages["design"].approved = True
    p.mark_completed("design", {})
    p.stages["implement"].approved = True
    await main.advance_pipeline(p)

    assert sent == []  # implement는 큐로 나가면 안 됨
    assert p.stages["implement"].status == StageStatus.RUNNING

    files = list(tmp_path.glob("p1_implement_implement_*.json"))
    assert len(files) == 1
    task = json.loads(files[0].read_text())
    assert task["project_id"] == "p1"
    assert task["stage"] == "implement"
    assert task["github_repo"] == "me/repo"


@pytest.mark.asyncio
async def test_qa_always_uses_queue_even_when_implement_is_manual(monkeypatch, tmp_path):
    """요구사항: implement만 외부 세션이 처리하고 QA 검증은 항상 정상 경로로 가야 한다."""
    monkeypatch.setattr(main, "project_manual_implement", {"p1": True})
    monkeypatch.setattr(main, "MANUAL_TASKS_DIR", str(tmp_path))
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append(agent_name)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = _project_ready_for_qa()
    await main.advance_pipeline(p)

    assert sent == ["qa"]  # implement가 아니라 qa라서 토글과 무관하게 큐로 나감
    assert list(tmp_path.glob("*.json")) == []


@pytest.mark.asyncio
async def test_retry_implement_with_feedback_respects_manual_toggle(monkeypatch, tmp_path):
    """QA 실패 후 재작업 요청(_retry_implement_with_feedback) — advance_pipeline
    최초 디스패치뿐 아니라 재시도 경로도 토글을 따라야 한다(재시도가 API 과금이
    가장 잦은 경로)."""
    monkeypatch.setattr(main, "project_manual_implement", {"p1": True})
    monkeypatch.setattr(main, "MANUAL_TASKS_DIR", str(tmp_path))
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append(agent_name)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = _project_ready_for_qa()
    await main._retry_implement_with_feedback(p, "다시 고쳐줘")

    assert sent == []
    assert list(tmp_path.glob("p1_implement_implement_*.json")) != []


@pytest.mark.asyncio
async def test_manual_toggle_off_uses_queue_as_before(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "project_manual_implement", {"p1": False})
    monkeypatch.setattr(main, "MANUAL_TASKS_DIR", str(tmp_path))
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append(agent_name)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = Pipeline("p1", "PRD 원본")
    p.mark_completed("planning", {})
    p.stages["design"].approved = True
    p.mark_completed("design", {})
    p.stages["implement"].approved = True
    await main.advance_pipeline(p)

    assert sent == ["implement"]
    assert list(tmp_path.glob("*.json")) == []


# ── manual-result 엔드포인트 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_manual_result_completes_stage_like_a_real_agent_would(monkeypatch):
    p = _project_ready_for_qa()
    p.stages["implement"].status = StageStatus.RUNNING
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main, "advance_pipeline", _noop)  # 다음 스테이지 디스패치는 범위 밖

    result = await main.manual_stage_result(
        "p1", "implement",
        main.ManualStageResult(agent="implement", outputs={"summary": "PR #1 생성", "branch": "b1", "pr_number": 1}),
    )

    assert result == {"ok": True}
    assert p.stages["implement"].status == StageStatus.COMPLETED
    assert p.stages["implement"].outputs["pr_number"] == 1


@pytest.mark.asyncio
async def test_manual_result_needs_rework_routes_to_implement_retry(monkeypatch):
    """QA(정상 경로로 컨테이너가 처리)가 실패+needs_rework를 보고하면 기존 라우팅
    (_route_needs_rework_or_fail → qa_retry_counts 증가 → implement 재작업 요청)을
    그대로 타야 한다 — manual-result 전용 분기를 새로 만들지 않았는지 확인."""
    p = _project_ready_for_qa()
    p.mark_running("qa")
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main, "qa_retry_counts", {})

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
