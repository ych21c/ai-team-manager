"""
회귀 테스트 — _retry_implement_with_feedback에 scenario_key를 넘기면
누적된 pipeline.instruction 전체(그동안의 모든 요구사항+피드백) 대신 "이
화면/기능만" 범위로 좁힌 instruction이 나가는지, scenario_key가 없을 때는
기존 동작(QA/AutoTest 재작업 루프가 의존하는 경로)이 바이트 단위로 그대로인지
확인한다.

이 경로가 조금이라도 달라지면 test_retry_instruction_guidance.py(되돌리지 말고
git log 먼저 확인하라는 가드레일)와 test_needs_rework_routing.py(QA/AutoTest
자동 재시도 루프)가 깨진다 — 그래서 scenario_key=None 쪽은 절대 안 건드렸다는
걸 여기서도 별도로 확인한다.

실행: cd orchestrator && pytest tests/test_scoped_implement_retry.py -v
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


async def test_scoped_retry_narrows_instruction_and_marks_scenario_key(monkeypatch):
    monkeypatch.setattr(main, "project_jira", {"p1": {"story_titles": {"ATM-5": "로그인 화면"}}})
    sent = {}

    async def _fake_send_task(agent_name, pid, task):
        sent.update(task)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = Pipeline("p1", "PRD: 전체 앱 요구사항 아주 길게...")
    p.mark_completed("implement", {"branch": "some-branch"})

    await main._retry_implement_with_feedback(p, "로고가 이상함", ["ATM-5"])

    instruction = sent["instruction"]
    assert "[범위 제한: ATM-5 - 로그인 화면]" in instruction
    assert "design/applied/ATM-5.html" in instruction
    assert "다른 화면/기능은 이미 반영돼 있으니 절대 건드리지 마세요" in instruction
    # 누적 PRD 전체가 그대로 다시 들어가면 안 됨 — 범위를 좁히는 의미가 없어짐
    assert "전체 앱 요구사항" not in instruction
    # git log 먼저 확인하라는 가드레일은 스코프 여부와 무관하게 항상 포함
    assert "git log" in instruction
    assert sent["context"]["scenario_keys"] == ["ATM-5"]


async def test_scoped_retry_with_multiple_keys_narrows_instruction_to_all(monkeypatch):
    """멀티선택 — 여러 화면을 같이 지정하면 instruction/context에 둘 다 반영돼야
    한다(첫 번째 키만 반영되던 회귀 방지)."""
    monkeypatch.setattr(main, "project_jira", {"p1": {"story_titles": {
        "ATM-5": "로그인 화면", "ATM-10": "시작 화면",
    }}})
    sent = {}

    async def _fake_send_task(agent_name, pid, task):
        sent.update(task)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = Pipeline("p1", "PRD: 전체 앱 요구사항 아주 길게...")
    p.mark_completed("implement", {"branch": "some-branch"})

    await main._retry_implement_with_feedback(p, "화면 2개 같이 손봄", ["ATM-5", "ATM-10"])

    instruction = sent["instruction"]
    assert "[범위 제한: ATM-5 - 로그인 화면, ATM-10 - 시작 화면]" in instruction
    assert "design/applied/ATM-5.html" in instruction
    assert "design/applied/ATM-10.html" in instruction
    assert sent["context"]["scenario_keys"] == ["ATM-5", "ATM-10"]


async def test_unscoped_retry_is_byte_identical_to_before(monkeypatch):
    """scenario_key를 안 주면(QA/AutoTest 자동 재작업 루프가 쓰는 경로) 예전
    동작과 완전히 동일해야 한다 — test_retry_instruction_guidance.py가 검증하는
    문구/구조가 그대로 있는지 여기서도 확인."""
    monkeypatch.setattr(main, "project_jira", {})
    sent = {}

    async def _fake_send_task(agent_name, pid, task):
        sent.update(task)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = Pipeline("p1", "카운터 앱 만들어줘")
    p.mark_completed("implement", {"branch": "some-branch"})

    await main._retry_implement_with_feedback(p, "ElevatedButton을 못 찾음")

    instruction = sent["instruction"]
    assert "카운터 앱 만들어줘" in instruction
    assert "[QA/AutoTest 재작업 요청]" in instruction
    assert "git log" in instruction
    assert "되돌리지 마세요" in instruction
    assert "ElevatedButton을 못 찾음" in instruction
    assert "범위 제한" not in instruction
    assert "scenario_keys" not in sent["context"]


async def test_unknown_scenario_key_falls_back_to_unscoped(monkeypatch):
    monkeypatch.setattr(main, "project_jira", {"p1": {"story_titles": {"ATM-5": "로그인 화면"}}})
    sent = {}

    async def _fake_send_task(agent_name, pid, task):
        sent.update(task)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    p = Pipeline("p1", "카운터 앱 만들어줘")
    p.mark_completed("implement", {"branch": "some-branch"})

    await main._retry_implement_with_feedback(p, "문제 있음", ["ATM-999"])

    instruction = sent["instruction"]
    assert "카운터 앱 만들어줘" in instruction
    assert "범위 제한" not in instruction
    assert "scenario_keys" not in sent["context"]
