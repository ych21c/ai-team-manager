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
  4. _send_task_or_manual이 디스패치 방식(큐/수동 파일)과 무관하게 항상
     stage.current_task에 에이전트별 태스크 스냅샷을 남기는지 — 플로우차트 탭이
     원시 로그 대신 구조화된 "지금 이 에이전트가 뭘 하는지"를 보여주는 기반.

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
    monkeypatch.setattr(main, "project_manual_qa_build", {})  # 기본은 꺼짐 — 각 테스트가 필요한 만큼만 켬


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


# ── current_task 스냅샷 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_current_task_recorded_for_queue_dispatch(monkeypatch):
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append(agent_name)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = Pipeline("p1", "지시사항 원본")
    await main.advance_pipeline(p)  # planning만 ready → pm 큐로

    task = p.stages["planning"].current_task["pm"]
    assert task["instruction"] == "지시사항 원본"
    assert task["manual"] is False
    assert isinstance(task["dispatched_at"], float)


@pytest.mark.asyncio
async def test_current_task_recorded_for_manual_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "project_manual_implement", {"p1": True})
    monkeypatch.setattr(main, "MANUAL_TASKS_DIR", str(tmp_path))

    p = Pipeline("p1", "PRD 원본")
    p.mark_completed("planning", {})
    p.stages["design"].approved = True
    p.mark_completed("design", {})
    p.stages["implement"].approved = True
    await main.advance_pipeline(p)

    task = p.stages["implement"].current_task["implement"]
    assert task["instruction"] == "PRD 원본"
    assert task["manual"] is True  # 큐로 안 나가도(파일로 대신 나가도) 스냅샷은 남아야 함


@pytest.mark.asyncio
async def test_current_task_tracked_per_agent_for_multi_agent_stage(monkeypatch):
    """design(designer+architect)처럼 에이전트가 여럿이면 각자 따로 기록돼야
    한다 — 하나로 합치면 나중에 dispatch된 쪽이 앞의 것을 덮어써 버린다."""
    monkeypatch.setattr(main.redis, "send_task", _noop)

    p = Pipeline("p1", "PRD 원본")
    p.mark_completed("planning", {})
    p.stages["design"].approved = True
    await main.advance_pipeline(p)  # design ready → designer+architect 둘 다 dispatch

    assert set(p.stages["design"].current_task.keys()) == {"designer", "architect"}


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


# ── project_manual_qa_build — QA 자체 테스트 코드 컴파일 실패 외부 처리 토글 ──

@pytest.mark.asyncio
async def test_set_manual_qa_build_toggles_and_exposes_in_project_info(monkeypatch):
    p = Pipeline("p1", "PRD 원본")
    monkeypatch.setattr(main, "projects", {"p1": p})

    result = await main.set_manual_qa_build("p1", main.ManualQaBuildToggle(enabled=True))

    assert result == {"ok": True, "manual_qa_build": True}
    assert main.project_manual_qa_build["p1"] is True
    assert main._make_project_info("p1")["manual_qa_build"] is True


@pytest.mark.asyncio
async def test_set_manual_qa_build_unknown_project_404s():
    with pytest.raises(main.HTTPException) as exc_info:
        await main.set_manual_qa_build("nope", main.ManualQaBuildToggle(enabled=True))
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_qa_dispatch_carries_manual_qa_build_flag_to_agent(monkeypatch):
    """QA는 큐로 나가지만(요구사항: 항상 정상 경로), agents/qa_testlab/run.py가
    자체 수정 예산 소진 시 외부로 넘길지 판단할 수 있도록 task payload에
    manual_qa_build_fix를 실어 보내야 한다 — 그 판단은 qa 컨테이너 안에서
    일어나서 orchestrator가 대신 가로챌 수 없다."""
    monkeypatch.setattr(main, "project_manual_qa_build", {"p1": True})
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append(task)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = _project_ready_for_qa()
    await main.advance_pipeline(p)

    assert len(sent) == 1
    assert sent[0]["manual_qa_build_fix"] is True


@pytest.mark.asyncio
async def test_retry_qa_with_feedback_carries_manual_qa_build_flag(monkeypatch):
    monkeypatch.setattr(main, "project_manual_qa_build", {"p1": True})
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append(task)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = _project_ready_for_qa()
    await main._retry_qa_with_feedback(p, "다시 확인해줘")

    assert len(sent) == 1
    assert sent[0]["manual_qa_build_fix"] is True


@pytest.mark.asyncio
async def test_qa_dispatch_flag_defaults_false_when_toggle_off(monkeypatch):
    sent = []

    async def _fake_send_task(agent_name, pid, task):
        sent.append(task)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = _project_ready_for_qa()
    await main.advance_pipeline(p)

    assert sent[0]["manual_qa_build_fix"] is False
