"""
회귀 테스트 — PM이 planning(재기획 포함)을 다시 돌 때 이미 만들어진 Jira 이슈
목록(existing_issues)을 context로 받는지 확인한다.

배경: PM이 재기획 때마다 같은 요구사항을 다른 문구로 다시 써서 내놓으면,
_sync_new_requirements_to_epic의 텍스트 기반 dedup(REQ ID/문구 비교)을
통과해버려 스프린트마다 똑같은 Jira 이슈가 중복 생성되는 문제가 있었다.
design 스테이지(scenarios)/채팅 트리아지(existing_issues)는 이미 기존 이슈
목록을 받고 있었는데 planning 본 실행 경로만 빠져 있었다 — 이제 동일한
_existing_issues_context 헬퍼로 통일한다.

실행: cd orchestrator && pytest tests/test_planning_existing_issues.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from workflows.pipeline import Pipeline

async def _noop(*args, **kwargs):
    pass


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    monkeypatch.setattr(main, "broadcast", _noop)
    monkeypatch.setattr(main, "_jira_stage_started", _noop)
    monkeypatch.setattr(main.spawner, "spawn_team", lambda *a, **k: None)
    monkeypatch.setattr(main, "project_repos", {})


# ── _existing_issues_context — 순수 헬퍼 ─────────────────────────────

def test_existing_issues_context_returns_key_title_pairs(monkeypatch):
    monkeypatch.setattr(main, "project_jira", {
        "p1": {"stories": ["ATM-5", "ATM-6"], "story_titles": {"ATM-5": "로그인 화면", "ATM-6": "홈 화면"}},
    })
    assert main._existing_issues_context("p1") == [
        {"key": "ATM-5", "title": "로그인 화면"},
        {"key": "ATM-6", "title": "홈 화면"},
    ]


def test_existing_issues_context_empty_when_no_stories(monkeypatch):
    monkeypatch.setattr(main, "project_jira", {})
    assert main._existing_issues_context("p1") == []


# ── advance_pipeline이 planning 태스크에 existing_issues를 실어 보내는지 ──

@pytest.mark.asyncio
async def test_advance_pipeline_includes_existing_issues_for_planning(monkeypatch):
    p = Pipeline("p1", "요구사항이 바뀜")
    monkeypatch.setattr(main, "project_jira", {
        "p1": {"epic": "ATM-1", "stories": ["ATM-5"], "story_titles": {"ATM-5": "로그인 화면"}},
    })
    sent = {}

    async def _fake_send_task(agent_name, pid, task):
        sent.update(task)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main.advance_pipeline(p)

    assert sent["context"]["existing_issues"] == [{"key": "ATM-5", "title": "로그인 화면"}]


@pytest.mark.asyncio
async def test_advance_pipeline_omits_existing_issues_for_first_planning_run(monkeypatch):
    p = Pipeline("p1", "새 프로젝트")
    monkeypatch.setattr(main, "project_jira", {})
    sent = {}

    async def _fake_send_task(agent_name, pid, task):
        sent.update(task)

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main.advance_pipeline(p)

    assert "existing_issues" not in sent["context"]
