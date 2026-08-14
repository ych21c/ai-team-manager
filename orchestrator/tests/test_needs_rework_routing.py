"""
회귀 테스트 — QA/AutoTest 실패를 Implement 재작업으로 라우팅하는 로직.
사용자 요청: "QA에서 문제 발생시 implement로 전달 → PR 생성 → CI 실패시 implement가
고쳐서 재시도 → 성공하면 머지" 체인이 실제로 동작해야 하고, QA와 AutoTest가
재시도 예산(MAX_QA_RETRIES)을 공유해서 무한 루프로 안 불어나야 한다.

실행: cd orchestrator && pytest tests/test_needs_rework_routing.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from workflows.pipeline import Pipeline, StageStatus

pytestmark = pytest.mark.asyncio


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


async def test_first_failure_triggers_implement_retry_not_hard_fail(_stub_side_effects):
    p = Pipeline("p1", "test")
    await main._route_needs_rework_or_fail(p, "p1", "qa", {"feedback": "빌드 실패"})
    assert _stub_side_effects == ["빌드 실패"]
    assert main.qa_retry_counts["p1"] == 1
    assert p.stages["qa"].status != StageStatus.FAILED


async def test_qa_and_autotest_share_the_same_retry_budget(_stub_side_effects):
    """핵심 회귀 테스트: QA 실패 재시도와 AutoTest 실패 재시도가 서로 다른
    카운터를 쓰면 총 재시도 횟수가 2배로 불어난다 — 같은 카운터를 공유해야 한다."""
    p = Pipeline("p1", "test")
    await main._route_needs_rework_or_fail(p, "p1", "qa", {"feedback": "a"})
    await main._route_needs_rework_or_fail(p, "p1", "autotest", {"feedback": "b"})
    assert main.qa_retry_counts["p1"] == 2
    assert len(_stub_side_effects) == 2


async def test_exceeding_max_retries_marks_failed_instead_of_retrying(_stub_side_effects):
    p = Pipeline("p1", "test")
    main.qa_retry_counts["p1"] = main.MAX_QA_RETRIES
    await main._route_needs_rework_or_fail(p, "p1", "autotest", {"feedback": "still broken"})
    # 재시도 함수는 더 안 불려야 하고, 스테이지는 FAILED로 남아야 한다.
    assert _stub_side_effects == []
    assert p.stages["autotest"].status == StageStatus.FAILED
