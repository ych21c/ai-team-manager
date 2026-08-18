"""
회귀 테스트 — pipeline.sprint ("전체 파이프라인 실행 회차" 카운터).

이 카운터는 _retry_planning_with_feedback(재기획 — planning부터 전체를
되돌리는 유일한 지점)에서만 증가하고, 그 값이 (1) STATE_DIR 스냅샷에
영속화돼 도커 재시작에도 살아남고, (2) 그 라운드에서 새로 생성되는
디자인 산출물(PR 제목/history 파일명/Jira 코멘트)과 신규 이슈 안내
메시지에 태그로 붙는지 확인한다.

실행: cd orchestrator && pytest tests/test_sprint_counter.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from workflows.pipeline import Pipeline


async def _noop(*args, **kwargs):
    pass


# ── Pipeline.sprint 기본값/직렬화 ────────────────────────────────────

def test_fresh_pipeline_starts_at_sprint_1():
    p = Pipeline("p1", "PRD")
    assert p.sprint == 1


def test_pipeline_accepts_restored_sprint_value():
    p = Pipeline("p1", "PRD", sprint=4)
    assert p.sprint == 4


def test_summary_includes_sprint_for_persistence():
    p = Pipeline("p1", "PRD", sprint=3)
    assert p.summary()["sprint"] == 3


# ── _load_all_projects — 도커 재시작 복원 ────────────────────────────

def test_load_all_projects_restores_sprint(tmp_path, monkeypatch):
    (tmp_path / "p1.json").write_text(
        '{"instruction": "PRD", "sprint": 5, "stages": {}}'
    )
    monkeypatch.setattr(main, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(main, "projects", {})
    monkeypatch.setattr(main, "project_names", {})
    monkeypatch.setattr(main, "project_repos", {})
    monkeypatch.setattr(main, "project_messages", {})
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main, "project_docs", {})
    monkeypatch.setattr(main, "project_token_totals", {})

    main._load_all_projects()

    assert main.projects["p1"].sprint == 5


def test_load_all_projects_defaults_sprint_to_1_for_old_snapshots(tmp_path, monkeypatch):
    """sprint 필드가 생기기 전에 저장된 스냅샷(옛 버전)을 읽어도 죽지 않고 1로 시작해야 한다."""
    (tmp_path / "p1.json").write_text('{"instruction": "PRD", "stages": {}}')
    monkeypatch.setattr(main, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(main, "projects", {})
    monkeypatch.setattr(main, "project_names", {})
    monkeypatch.setattr(main, "project_repos", {})
    monkeypatch.setattr(main, "project_messages", {})
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main, "project_docs", {})
    monkeypatch.setattr(main, "project_token_totals", {})

    main._load_all_projects()

    assert main.projects["p1"].sprint == 1


# ── _retry_planning_with_feedback — 유일한 증가 지점 ─────────────────

@pytest.mark.asyncio
async def test_retry_planning_increments_sprint(monkeypatch):
    p = Pipeline("p1", "PRD")
    assert p.sprint == 1
    monkeypatch.setattr(main, "advance_pipeline", _noop)

    await main._retry_planning_with_feedback(p, "요구사항이 바뀜", full_rewrite=False)

    assert p.sprint == 2


@pytest.mark.asyncio
async def test_retry_planning_increments_sprint_again_on_second_call(monkeypatch):
    p = Pipeline("p1", "PRD", sprint=3)
    monkeypatch.setattr(main, "advance_pipeline", _noop)

    await main._retry_planning_with_feedback(p, "또 바뀜", full_rewrite=True)

    assert p.sprint == 4


@pytest.mark.asyncio
async def test_retry_design_with_feedback_does_not_touch_sprint(monkeypatch):
    """design 재작업(스코프 재시도)은 "전체 파이프라인 회차"가 아니므로 sprint를
    건드리면 안 된다 — planning 재기획과 구분되는 지점임을 못박아둔다."""
    p = Pipeline("p1", "PRD")
    p.mark_completed("planning", {})
    p.mark_completed("design", {})
    p.stages["implement"].approved = True
    p.mark_completed("implement", {})
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main, "project_repos", {})
    monkeypatch.setattr(main.spawner, "spawn_team", lambda *a, **k: None)
    monkeypatch.setattr(main.redis, "send_task", _noop)
    monkeypatch.setattr(main, "broadcast", _noop)

    await main._retry_design_with_feedback(p, "버튼이 이상해요")

    assert p.sprint == 1


# ── publish_design — 산출물에 sprint 태그 ────────────────────────────

@pytest.mark.asyncio
async def test_publish_design_tags_pr_title_and_history_filename_with_sprint(monkeypatch, tmp_path):
    # publish_design은 workspace 경로를 "/workspace/{project_id}"로 직접 만들기 때문에
    # (인자로 안 받음) 실제 그 경로 밑에 pending 목업을 만들어두고 끝나면 지운다 —
    # 이 함수만을 위해 main.py를 고치기보다는, 실제 런타임과 동일한 경로로 검증한다.
    import shutil

    pid = "test-sprint-pub"
    workspace = f"/workspace/{pid}"
    pending = f"{workspace}/design/pending"
    os.makedirs(pending, exist_ok=True)
    with open(f"{pending}/ATM-5.html", "w") as f:
        f.write("<button>새 목업</button>")

    monkeypatch.setattr(main, "projects", {pid: Pipeline(pid, "PRD", sprint=3)})
    monkeypatch.setattr(main, "project_jira", {})
    monkeypatch.setattr(main, "broadcast", _noop)
    monkeypatch.setattr(main, "add_jira_comment", _noop)

    captured_pr = {}

    async def _fake_create_pr(repo, branch, title, body):
        captured_pr["title"] = title
        return 42

    async def _fake_merge(repo, pr_number):
        return True

    monkeypatch.setattr(main, "create_pull_request", _fake_create_pr)
    monkeypatch.setattr(main, "merge_pull_request", _fake_merge)

    try:
        result = await main.publish_design(pid, main.DesignPublish(
            github_repo="me/repo", branch=f"design/{pid}-1", scenarios=["ATM-5"],
        ))

        assert result["merged"] is True
        assert "sprint 3" in captured_pr["title"]

        history_dir = f"{workspace}/design/history/ATM-5"
        saved = [f for f in os.listdir(history_dir) if f.startswith("sprint3_")]
        assert len(saved) == 1
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ── 채팅 트리아지로 만들어지는 신규 이슈 — sprint 태그 ────────────────

@pytest.mark.asyncio
async def test_create_ad_hoc_jira_story_tags_message_with_current_sprint(monkeypatch):
    pid = "p1"
    monkeypatch.setattr(main, "projects", {pid: Pipeline(pid, "PRD", sprint=7)})
    monkeypatch.setattr(main, "project_jira", {pid: {"epic": "ATM-1"}})
    monkeypatch.setattr(main, "project_names", {})

    async def _fake_create_jira_stories(epic, pname, titles):
        return [{"key": "ATM-9", "title": titles[0]}]

    monkeypatch.setattr(main, "create_jira_stories", _fake_create_jira_stories)

    broadcasts = []

    async def _capture_broadcast(event):
        broadcasts.append(event)

    monkeypatch.setattr(main, "broadcast", _capture_broadcast)

    key = await main._create_ad_hoc_jira_story(pid, "새 화면")

    assert key == "ATM-9"
    msg = broadcasts[-1]["content"]
    assert "Sprint 7" in msg
    assert "ATM-9" in msg
