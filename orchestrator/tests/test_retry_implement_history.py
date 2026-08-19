"""
회귀 테스트 — POST /projects/{id}/retry-implement (QA 재시도 예산 소진 후 사람이
직접 개입하는 수동 엔드포인트)도 자동 루프(_route_needs_rework_or_fail)처럼
프로젝트 히스토리에 "왜 implement가 재실행됐는지"를 남겨야 한다. 이 엔드포인트는
_add_history를 호출하지 않아서, 자동 루프가 이미 3번 실패하고 사람이 원인 파악 후
넣은 재작업 요청만 스프린트 히스토리에서 누락되는 문제가 있었다.

실행: cd orchestrator && pytest tests/test_retry_implement_history.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from workflows.pipeline import Pipeline

pytestmark = pytest.mark.asyncio


async def _noop(*args, **kwargs):
    pass


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    monkeypatch.setattr(main, "broadcast", _noop)
    monkeypatch.setattr(main, "project_repos", {})
    monkeypatch.setattr(main, "project_jira", {})


async def test_retry_implement_endpoint_logs_reason_to_history(monkeypatch):
    p = Pipeline("p1", "카운터 앱 만들어줘")
    p.mark_completed("implement", {"branch": "some-branch"})
    monkeypatch.setattr(main, "projects", {"p1": p})

    history_calls = []

    async def _fake_add_history(pid, entry):
        history_calls.append((pid, entry))
    monkeypatch.setattr(main, "_add_history", _fake_add_history)

    retried = []

    async def _fake_retry(pipeline, feedback, scenario_keys=None):
        retried.append((feedback, scenario_keys))
    monkeypatch.setattr(main, "_retry_implement_with_feedback", _fake_retry)

    body = main.RetryFeedback(feedback="디바이스 목록 화면에서 크래시남")
    await main.retry_implement("p1", body)

    assert len(history_calls) == 1
    pid, entry = history_calls[0]
    assert pid == "p1"
    assert "디바이스 목록 화면에서 크래시남" in entry
    assert retried == [("디바이스 목록 화면에서 크래시남", None)]


async def test_retry_implement_endpoint_unknown_project_raises_before_history(monkeypatch):
    monkeypatch.setattr(main, "projects", {})
    history_calls = []

    async def _fake_add_history(pid, entry):
        history_calls.append((pid, entry))
    monkeypatch.setattr(main, "_add_history", _fake_add_history)

    with pytest.raises(main.HTTPException):
        await main.retry_implement("missing", main.RetryFeedback(feedback="x"))

    assert history_calls == []
