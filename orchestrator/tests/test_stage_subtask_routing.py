"""
회귀 테스트 — Story(요구사항) 밑에 design/implement/qa 하위 작업이 생기면서,
각 단계가 자기 하위 작업만 업데이트하고 story 자체에는 코멘트가 안 남는지
확인한다(하위 작업이 없는 예전 프로젝트/스테이지는 story로 폴백).

배경: 예전엔 design/implement/qa가 전부 같은 story 이슈에 코멘트를 쌓아서,
Jira만 보고는 "디자인은 끝났는데 구현은 아직인지" 단계별 진행 상황을 구분할
수 없었다. _stage_issue_target이 이 라우팅을 담당하고, main.py의 여러
Jira 업데이트 지점(_jira_stage_started/implement·qa 완료 처리/design 목업
반영 코멘트)이 전부 이 함수를 거치도록 바꿨다.

실행: cd orchestrator && pytest tests/test_stage_subtask_routing.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from workflows.pipeline import Pipeline


async def _noop(*args, **kwargs):
    pass


# ── _stage_issue_target — 순수 라우팅 로직 ───────────────────────────

def test_routes_to_stage_subtask_when_present(monkeypatch):
    monkeypatch.setattr(main, "project_jira", {
        "p1": {"story_subtasks": {"ATM-2": {"design": "ATM-3", "implement": "ATM-4", "qa": "ATM-5"}}},
    })
    assert main._stage_issue_target("p1", "ATM-2", "design") == "ATM-3"
    assert main._stage_issue_target("p1", "ATM-2", "implement") == "ATM-4"
    assert main._stage_issue_target("p1", "ATM-2", "qa") == "ATM-5"


def test_falls_back_to_story_when_no_subtasks(monkeypatch):
    """하위 작업 도입 전에 만들어진 스토리(story_subtasks에 항목 없음)는
    예전처럼 story 자체에 코멘트가 남아야 한다."""
    monkeypatch.setattr(main, "project_jira", {"p1": {"story_subtasks": {}}})
    assert main._stage_issue_target("p1", "ATM-2", "design") == "ATM-2"


def test_falls_back_to_story_for_stages_without_subtasks(monkeypatch):
    """autotest/release는 애초에 design/implement/qa 3종 하위 작업에 안 속하므로
    story 자체가 대상이어야 한다."""
    monkeypatch.setattr(main, "project_jira", {
        "p1": {"story_subtasks": {"ATM-2": {"design": "ATM-3", "implement": "ATM-4", "qa": "ATM-5"}}},
    })
    assert main._stage_issue_target("p1", "ATM-2", "autotest") == "ATM-2"
    assert main._stage_issue_target("p1", "ATM-2", "release") == "ATM-2"


def test_falls_back_to_story_when_project_has_no_jira(monkeypatch):
    monkeypatch.setattr(main, "project_jira", {})
    assert main._stage_issue_target("unknown", "ATM-2", "qa") == "ATM-2"


# ── story 생성 경로가 story_subtasks를 project_jira에 반영하는지 ──────

@pytest.mark.asyncio
async def test_sync_new_requirements_propagates_subtasks(monkeypatch):
    monkeypatch.setattr(main, "project_jira", {"p1": {"epic": "ATM-1"}})
    monkeypatch.setattr(main, "project_names", {})

    async def _fake_create_jira_stories(epic, pname, reqs):
        return [{"key": "ATM-2", "title": "로그인", "subtasks": {"design": "ATM-3", "implement": "ATM-4", "qa": "ATM-5"}}]

    monkeypatch.setattr(main, "create_jira_stories", _fake_create_jira_stories)
    monkeypatch.setattr(main, "parse_pm_requirements", lambda text, name: (name, ["로그인"]))

    await main._sync_new_requirements_to_epic("p1", "PRD 텍스트")

    assert main.project_jira["p1"]["story_subtasks"]["ATM-2"] == {
        "design": "ATM-3", "implement": "ATM-4", "qa": "ATM-5",
    }


@pytest.mark.asyncio
async def test_ad_hoc_jira_story_propagates_subtasks(monkeypatch):
    monkeypatch.setattr(main, "project_jira", {"p1": {"epic": "ATM-1"}})
    monkeypatch.setattr(main, "project_names", {})
    monkeypatch.setattr(main, "projects", {})
    monkeypatch.setattr(main, "broadcast", _noop)

    async def _fake_create_jira_stories(epic, pname, titles):
        return [{"key": "ATM-9", "title": titles[0], "subtasks": {"design": "ATM-10", "implement": "ATM-11", "qa": "ATM-12"}}]

    monkeypatch.setattr(main, "create_jira_stories", _fake_create_jira_stories)

    key = await main._create_ad_hoc_jira_story("p1", "새 화면")

    assert key == "ATM-9"
    assert main.project_jira["p1"]["story_subtasks"]["ATM-9"] == {
        "design": "ATM-10", "implement": "ATM-11", "qa": "ATM-12",
    }


# ── handle_agent_event 실제 경로 — story가 아니라 해당 stage의 하위 작업을 건드리는지 ──

@pytest.mark.asyncio
async def test_implement_completion_updates_implement_subtask_not_story(monkeypatch):
    p = Pipeline("p1", "PRD")
    p.mark_completed("planning", {})
    p.mark_completed("design", {})
    p.stages["implement"].approved = True

    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main, "project_jira", {
        "p1": {"stories": ["ATM-2"], "story_subtasks": {"ATM-2": {"implement": "ATM-4"}}},
    })
    monkeypatch.setattr(main, "project_repos", {"p1": "me/repo"})
    monkeypatch.setattr(main, "broadcast", _noop)
    monkeypatch.setattr(main, "_add_history", _noop)
    monkeypatch.setattr(main, "advance_pipeline", _noop)  # 다음 스테이지 디스패치는 이 테스트 범위 밖

    calls = []

    async def _fake_update_status(issue_key, status):
        calls.append(("status", issue_key, status))

    async def _fake_comment(issue_key, text):
        calls.append(("comment", issue_key, text))

    monkeypatch.setattr(main, "update_jira_status", _fake_update_status)
    monkeypatch.setattr(main, "add_jira_comment", _fake_comment)

    await main.handle_agent_event({
        "project_id": "p1", "agent": "implement", "type": "stage_completed",
        "stage": "implement", "outputs": {"pr_url": "https://github.com/me/repo/pull/1"},
    })

    assert calls == [
        ("status", "ATM-4", "In Progress"),
        ("comment", "ATM-4", "🤖 구현 완료 — PR: https://github.com/me/repo/pull/1"),
    ]
