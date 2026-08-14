"""
회귀 테스트 — 채팅 트리아지가 판단한 target(기존 이슈 key | "new" | None)이
실제 scenario_key로 올바르게 해석되는지 확인한다.

배경: "1번 화면 로고가 이상해서 다시 디자인해줘" 같은 요청을 이슈 단위로
좁히려면(test_scoped_design_retry.py) 채팅 트리아지가 "이게 어떤 이슈를
가리키는지" 먼저 판단해야 한다. PM 트리아지(agent.py, parse_triage_decision)는
project_jira 상태를 모르는 별도 프로세스라 target이 실제 존재하는 키인지
검증할 수 없다 — 그 검증은 _handle_chat_triage_result가 한다. 여기서는 그
검증/라우팅 로직만 본다(디자인/구현 재작업 자체의 상세 동작은
test_scoped_design_retry.py/test_scoped_implement_retry.py가 검증).

실행: cd orchestrator && pytest tests/test_chat_triage_scoping.py -v
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


def _project_with_scenarios(pid: str) -> Pipeline:
    p = Pipeline(pid, "PRD 원본")
    p.mark_completed("planning", {})
    p.mark_completed("design", {"design_preview": True})
    p.stages["implement"].approved = True
    p.mark_completed("implement", {"branch": "b1", "pr_number": 1})
    p.mark_completed("qa", {"passed": True})
    p.mark_completed("autotest", {"passed": True})
    p.mark_waiting_approval("release")
    return p


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    monkeypatch.setattr(main, "broadcast", _noop)
    monkeypatch.setattr(main, "_add_history", _noop)
    monkeypatch.setattr(main, "chat_triage_in_flight", {})


# ── existing_issues가 pm 트리아지 태스크로 넘어가는지 ─────────────────────

async def test_dispatch_includes_existing_issues_when_stories_present(monkeypatch):
    p = _project_with_scenarios("p1")
    monkeypatch.setattr(main, "project_jira", {
        "p1": {"stories": ["ATM-5"], "story_titles": {"ATM-5": "로그인 화면"}},
    })
    monkeypatch.setattr(main, "project_repos", {})
    sent = {}

    async def _fake_send_task(agent_name, pid, task):
        sent.update(task)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main._dispatch_chat_triage(p, "1번 화면 로고가 이상해요")

    assert sent["context"]["existing_issues"] == [{"key": "ATM-5", "title": "로그인 화면"}]


async def test_dispatch_omits_existing_issues_when_no_stories(monkeypatch):
    p = _project_with_scenarios("p1")
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main, "project_repos", {})
    sent = {}

    async def _fake_send_task(agent_name, pid, task):
        sent.update(task)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main._dispatch_chat_triage(p, "화면 하나 새로 만들어줘")

    assert "existing_issues" not in sent["context"]


# ── target 해석: 기존 키 / new / 알 수 없는 값 ────────────────────────────

async def test_target_existing_key_is_passed_through_as_scenario_key(monkeypatch):
    p = _project_with_scenarios("p1")
    monkeypatch.setattr(main, "project_jira", {
        "p1": {"stories": ["ATM-5"], "story_titles": {"ATM-5": "로그인 화면"}},
    })

    calls = []

    async def _fake_retry_design(pipeline, feedback, scenario_key=None):
        calls.append(scenario_key)

    monkeypatch.setattr(main, "_retry_design_with_feedback", _fake_retry_design)

    await main._handle_chat_triage_result(p, "p1", {
        "scope": "design", "target": "ATM-5", "new_story_title": "",
        "feedback": "로고가 이상함", "reply": "ATM-5만 다시 만들게요",
    })

    assert calls == ["ATM-5"]


async def test_target_new_creates_jira_story_then_scopes_to_it(monkeypatch):
    p = _project_with_scenarios("p1")
    monkeypatch.setattr(main, "project_jira", {
        "p1": {"epic": "ATM-1", "stories": ["ATM-5"], "story_titles": {"ATM-5": "로그인 화면"}},
    })

    async def _fake_create_jira_stories(epic_key, project_name, requirements):
        assert epic_key == "ATM-1"
        assert requirements == ["다크모드 지원"]
        return [{"key": "ATM-9", "title": "다크모드 지원"}]

    monkeypatch.setattr(main, "create_jira_stories", _fake_create_jira_stories)

    calls = []

    async def _fake_retry_design(pipeline, feedback, scenario_key=None):
        calls.append(scenario_key)

    monkeypatch.setattr(main, "_retry_design_with_feedback", _fake_retry_design)

    await main._handle_chat_triage_result(p, "p1", {
        "scope": "design", "target": "new", "new_story_title": "다크모드 지원",
        "feedback": "다크모드 추가해줘", "reply": "새 이슈로 등록하고 만들게요",
    })

    # 새 이슈가 실제로 project_jira에 반영됐는지 (epic 등 기존 필드는 안 건드림)
    jira = main.project_jira["p1"]
    assert "ATM-9" in jira["stories"]
    assert jira["story_titles"]["ATM-9"] == "다크모드 지원"
    assert jira["epic"] == "ATM-1"
    # 새로 만든 키로 스코프가 좁혀졌는지
    assert calls == ["ATM-9"]


async def test_target_unknown_key_falls_back_to_unscoped(monkeypatch):
    p = _project_with_scenarios("p1")
    monkeypatch.setattr(main, "project_jira", {
        "p1": {"stories": ["ATM-5"], "story_titles": {"ATM-5": "로그인 화면"}},
    })

    calls = []

    async def _fake_retry_implement(pipeline, feedback, scenario_key=None):
        calls.append(scenario_key)

    monkeypatch.setattr(main, "_retry_implement_with_feedback", _fake_retry_implement)

    await main._handle_chat_triage_result(p, "p1", {
        "scope": "implement", "target": "ATM-999", "new_story_title": "",
        "feedback": "버튼이 안 눌림", "reply": "구현 다시 볼게요",
    })

    assert calls == [None]


async def test_no_target_falls_back_to_unscoped(monkeypatch):
    p = _project_with_scenarios("p1")
    monkeypatch.setattr(main, "project_jira", {})

    calls = []

    async def _fake_retry_design(pipeline, feedback, scenario_key=None):
        calls.append(scenario_key)

    monkeypatch.setattr(main, "_retry_design_with_feedback", _fake_retry_design)

    await main._handle_chat_triage_result(p, "p1", {
        "scope": "design", "feedback": "디자인이 이상함", "reply": "다시 만들게요",
    })

    assert calls == [None]
