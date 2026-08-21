"""
회귀 테스트 — QA/AutoTest 실패를 Implement 재작업으로 라우팅하는 로직.
사용자 요청: "QA에서 문제 발생시 implement로 전달 → PR 생성 → CI 실패시 implement가
고쳐서 재시도 → 성공하면 머지" 체인이 실제로 동작해야 하고, QA와 AutoTest가
재시도 예산(MAX_QA_RETRIES)을 공유해서 무한 루프로 안 불어나야 한다.

실행: cd orchestrator && pytest tests/test_needs_rework_routing.py -v
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from workflows.pipeline import Pipeline, StageStatus


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    """Jira/Confluence/WebSocket 브로드캐스트 없이 순수 라우팅 로직만 검증한다."""
    monkeypatch.setattr(main, "broadcast", _noop)
    monkeypatch.setattr(main, "add_jira_comment", _noop)
    monkeypatch.setattr(main, "_add_history", _noop)
    monkeypatch.setattr(main, "project_jira", {})
    retried = []

    async def _fake_retry(pipeline, feedback):
        retried.append(feedback)
    monkeypatch.setattr(main, "_retry_implement_with_feedback", _fake_retry)
    main.qa_retry_counts.clear()
    return retried


async def _noop(*args, **kwargs):
    pass


@pytest.mark.asyncio
async def test_first_failure_triggers_implement_retry_not_hard_fail(_stub_side_effects):
    p = Pipeline("p1", "test")
    await main._route_needs_rework_or_fail(p, "p1", "qa", {"feedback": "빌드 실패"})
    assert _stub_side_effects == ["빌드 실패"]
    assert main.qa_retry_counts["p1"] == 1
    assert p.stages["qa"].status != StageStatus.FAILED


@pytest.mark.asyncio
async def test_qa_and_autotest_share_the_same_retry_budget(_stub_side_effects):
    """핵심 회귀 테스트: QA 실패 재시도와 AutoTest 실패 재시도가 서로 다른
    카운터를 쓰면 총 재시도 횟수가 2배로 불어난다 — 같은 카운터를 공유해야 한다."""
    p = Pipeline("p1", "test")
    await main._route_needs_rework_or_fail(p, "p1", "qa", {"feedback": "a"})
    await main._route_needs_rework_or_fail(p, "p1", "autotest", {"feedback": "b"})
    assert main.qa_retry_counts["p1"] == 2
    assert len(_stub_side_effects) == 2


@pytest.mark.asyncio
async def test_exceeding_max_retries_marks_failed_instead_of_retrying(_stub_side_effects):
    p = Pipeline("p1", "test")
    main.qa_retry_counts["p1"] = main.MAX_QA_RETRIES
    await main._route_needs_rework_or_fail(p, "p1", "autotest", {"feedback": "still broken"})
    # 재시도 함수는 더 안 불려야 하고, 스테이지는 FAILED로 남아야 한다.
    assert _stub_side_effects == []
    assert p.stages["autotest"].status == StageStatus.FAILED


@pytest.mark.asyncio
async def test_exceeding_max_retries_with_manual_implement_routes_to_external_task(_stub_side_effects, monkeypatch):
    """recoveryfit에서 실제로 재현: manual_implement가 켜진 프로젝트는 예산이
    소진돼도 그냥 멈추지 않는다 — 사람이 API를 수동 호출하길 기다리는 대신
    MANUAL_TASKS_DIR 외부 작업 요청 경로(_retry_implement_with_feedback →
    _send_task_or_manual)로 계속 진행하고, 카운터를 0으로 리셋해 다음
    MAX_QA_RETRIES 예산을 새로 준다."""
    monkeypatch.setattr(main, "project_manual_implement", {"p1": True})
    p = Pipeline("p1", "test")
    main.qa_retry_counts["p1"] = main.MAX_QA_RETRIES
    await main._route_needs_rework_or_fail(p, "p1", "qa", {"feedback": "스플래시→랜딩 전환 실패"})
    assert _stub_side_effects == ["스플래시→랜딩 전환 실패"]
    assert main.qa_retry_counts["p1"] == 0
    assert p.stages["qa"].status != StageStatus.FAILED


# ── qa_retry_counts 영속화 — reload/재시작에도 예산 가드가 살아남아야 함 ──
#
# recoveryfit(c052dd6b)에서 실제로 재현된 사고: orchestrator/main.py를 바인드
# 마운트로 라이브 편집(self-improve 프로젝트가 자기 소스를 고치는 중)할 때마다
# uvicorn --reload가 프로세스를 재시작해서 qa_retry_counts가 메모리에만 있던
# 시절엔 조용히 0으로 리셋됐다. 그래서 같은 지점(스플래시→랜딩 전환 시나리오)
# 에서 몇 시간째 반복 실패해도 "1/3"만 계속 찍히고 MAX_QA_RETRIES 가드가 한
# 번도 발동하지 못해, 사람이 수동으로 취소할 때까지 안 멈췄다.

def test_save_project_persists_qa_retry_count(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "STATE_DIR", str(tmp_path))
    p = Pipeline("p1", "PRD")
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main, "project_names", {})
    monkeypatch.setattr(main, "project_repos", {})
    monkeypatch.setattr(main, "project_messages", {})
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main, "project_docs", {})
    monkeypatch.setattr(main, "project_token_totals", {})
    monkeypatch.setattr(main, "project_deploy_config", {})
    monkeypatch.setattr(main, "project_deploy_status", {})
    monkeypatch.setattr(main, "qa_retry_counts", {"p1": 2})

    main._save_project("p1")

    saved = json.loads((tmp_path / "p1.json").read_text())
    assert saved["qa_retry_count"] == 2


def test_load_all_projects_restores_qa_retry_count(tmp_path, monkeypatch):
    (tmp_path / "p1.json").write_text(
        '{"instruction": "PRD", "sprint": 1, "stages": {}, "qa_retry_count": 2}'
    )
    monkeypatch.setattr(main, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(main, "projects", {})
    monkeypatch.setattr(main, "project_names", {})
    monkeypatch.setattr(main, "project_repos", {})
    monkeypatch.setattr(main, "project_messages", {})
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main, "project_docs", {})
    monkeypatch.setattr(main, "project_token_totals", {})
    monkeypatch.setattr(main, "project_deploy_config", {})
    monkeypatch.setattr(main, "project_deploy_status", {})
    monkeypatch.setattr(main, "qa_retry_counts", {})

    main._load_all_projects()

    assert main.qa_retry_counts["p1"] == 2
