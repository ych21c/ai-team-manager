"""
회귀 테스트 — Implement 재작업 요청에 "테스트 실패 = 무조건 앱을 되돌려라"가
아니라 최근 커밋 의도부터 확인하라는 판단 기준이 실제로 포함되는지 확인한다.

실제로 있었던 사고: 디자인을 스펙대로(AppBar/FloatingActionButton) 고친 직후
AutoTest가 옛날 테스트(ElevatedButton 기준) 실패를 재작업 요청으로 보냈는데,
아무 판단 기준 없이 로그만 넘겨서 Implement가 새 디자인을 도로 옛날 걸로
되돌려버렸다. git log를 먼저 보고 판단하라는 기준이 있었으면 안 그랬을 것.

실행: cd orchestrator && pytest tests/test_retry_instruction_guidance.py -v
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


async def test_retry_instruction_warns_against_blind_revert(monkeypatch):
    sent = {}

    async def _fake_send_task(agent_name, pid, task):
        sent.update(task)

    monkeypatch.setattr(main, "broadcast", _noop)
    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)
    monkeypatch.setattr(main, "project_repos", {})

    p = Pipeline("p1", "카운터 앱 만들어줘")
    p.mark_completed("implement", {"branch": "some-branch"})

    await main._retry_implement_with_feedback(p, "ElevatedButton을 못 찾음")

    instruction = sent["instruction"]
    assert "git log" in instruction
    assert "되돌리지 마세요" in instruction
    assert "ElevatedButton을 못 찾음" in instruction
