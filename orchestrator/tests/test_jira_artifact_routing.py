"""
회귀 테스트 — 산출물(디자인 목업/QA 녹화)을 Jira 이슈에 남기는 대상을 고르는
순수 로직. 실제 산출물을 어느 라운드에 만들었는지와 무관하게, 앱 안에서는
qa_recording.mp4/design/applied/{key}.html처럼 최신 하나만 남는(덮어써지는)
파일이 있어서 — Jira 코멘트/첨부만이 라운드별 이력을 보존한다. 이 파일은 그
"어디에 남길지" 결정 로직만 다룬다(실제 Jira HTTP 호출은 목킹하지 않음 — 이
저장소의 다른 테스트들과 같은 관례).

실행: cd orchestrator && pytest tests/test_jira_artifact_routing.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _implement_jira_comment_targets, _scenarios_with_jira_issue


# ── _implement_jira_comment_targets ─────────────────────────────────────────

def test_scoped_retry_targets_only_that_issue():
    assert _implement_jira_comment_targets(["ATM-5"], ["ATM-3", "ATM-5", "ATM-9"]) == ["ATM-5"]


def test_scoped_retry_with_multiple_keys_targets_only_those_issues():
    """멀티선택 — 여러 키로 범위를 좁히면 그 이슈들에만 코멘트를 남겨야 한다."""
    assert _implement_jira_comment_targets(["ATM-5", "ATM-9"], ["ATM-3", "ATM-5", "ATM-9"]) == ["ATM-5", "ATM-9"]


def test_unscoped_targets_all_stories():
    """범위 제한 없는 초기 구현 등은 PR이 모든 화면에 영향을 주므로 전체
    스토리에 코멘트를 남겨야 한다 — 예전 stories[0] 전용 버그의 회귀 방지."""
    assert _implement_jira_comment_targets(None, ["ATM-3", "ATM-5", "ATM-9"]) == ["ATM-3", "ATM-5", "ATM-9"]


def test_unknown_scenario_key_falls_back_to_all_stories():
    """PM이 잘못 판단했거나 project_jira 상태와 어긋난 키는 실제 이슈가
    아니므로, 조용히 전체 스토리로 폴백해야 한다(엉뚱한 이슈에 코멘트가
    달리는 것보다 안전)."""
    assert _implement_jira_comment_targets(["ATM-999"], ["ATM-3", "ATM-5"]) == ["ATM-3", "ATM-5"]


def test_no_stories_returns_empty():
    assert _implement_jira_comment_targets(["ATM-5"], []) == []
    assert _implement_jira_comment_targets(None, []) == []


# ── _scenarios_with_jira_issue ──────────────────────────────────────────────

def test_only_real_jira_issues_are_commentable():
    result = _scenarios_with_jira_issue(["ATM-5", "ATM-6"], ["ATM-5", "ATM-6", "ATM-7"])
    assert result == ["ATM-5", "ATM-6"]


def test_fallback_main_key_excluded_when_jira_disabled():
    """Jira 연동이 꺼져 있으면(story 목록이 비어 있음) design 스테이지가 쓰는
    "main" 폴백 키는 실제 이슈가 아니므로 코멘트 대상에서 빠져야 한다."""
    assert _scenarios_with_jira_issue(["main"], []) == []


def test_partial_match_keeps_only_known_issues():
    """PM이 판단한 target이 project_jira 상태와 어긋나 존재하지 않는 키가
    섞여 들어와도, 실제 존재하는 이슈만 걸러내야 한다."""
    result = _scenarios_with_jira_issue(["ATM-5", "ATM-999"], ["ATM-5", "ATM-6"])
    assert result == ["ATM-5"]


def test_empty_scenarios_returns_empty():
    assert _scenarios_with_jira_issue([], ["ATM-5"]) == []
