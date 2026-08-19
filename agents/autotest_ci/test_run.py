"""
회귀 테스트 — AutoTest(CI 폴러)가 실패를 감지하고도 "실패한 체크: analyze-and-test"
라는 요약만 남기고 파이프라인을 멈추던 문제. 사용자 요청: CI가 실패하면 실제 로그를
Implement에 넘겨서 재작업 체인이 자동으로 돌게 해야 한다.

실행: cd agents/autotest_ci && pip install pytest && pytest test_run.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GITHUB_TOKEN", "x")

from run import _pick_failed_run, _extract_job_id, _find_upstream, _extract_issue_lines


def test_pick_failed_run_finds_failure_conclusion():
    runs = [
        {"name": "a", "conclusion": "success"},
        {"name": "b", "conclusion": "failure"},
    ]
    assert _pick_failed_run(runs)["name"] == "b"


def test_pick_failed_run_ignores_skipped_and_neutral():
    runs = [{"name": "a", "conclusion": "skipped"}, {"name": "b", "conclusion": "neutral"}]
    assert _pick_failed_run(runs) is None


def test_pick_failed_run_none_when_all_success():
    assert _pick_failed_run([{"name": "a", "conclusion": "success"}]) is None


def test_extract_job_id_from_real_url_shape():
    url = "https://github.com/ych21c/counter-app/actions/runs/31249972900/job/88888888"
    assert _extract_job_id(url) == "88888888"


def test_extract_job_id_none_for_unrelated_url():
    assert _extract_job_id("https://github.com/ych21c/counter-app/pulls/9") is None


def test_extract_job_id_none_for_empty():
    assert _extract_job_id("") is None
    assert _extract_job_id(None) is None


def test_find_upstream_prefers_implement_over_stale_autotest():
    """핵심 회귀 테스트: QA와 같은 클래스의 버그 — autotest 자신의 예전
    completed 결과(branch/head_sha/pr_number)가 context에 남아있으면 방금
    implement가 만든 새 값을 덮어쓰던 사고."""
    context = {
        "implement": {"branch": "new-branch", "head_sha": "newsha", "pr_number": 11},
        "autotest": {"branch": "ancient-stale-branch", "head_sha": "oldsha", "pr_number": 6},
    }
    assert _find_upstream(context, "branch") == "new-branch"
    assert _find_upstream(context, "head_sha") == "newsha"
    assert _find_upstream(context, "pr_number") == 11


def test_find_upstream_falls_back_when_no_implement():
    context = {"autotest": {"branch": "only-option"}}
    assert _find_upstream(context, "branch") == "only-option"


def test_extract_issue_lines_keeps_analyzer_error_drops_noise():
    """예전 버그: 로그 끝 2000자만 자르면 flutter analyze 에러가 설치/캐시 로그
    뒤에 묻혀 잘려나갔다. error/warning 줄과 그 문맥만 남아야 한다."""
    log = "\n".join([
        "2024-01-01T00:00:00Z Run flutter pub get",
        "2024-01-01T00:00:00Z Resolving dependencies...",
        "2024-01-01T00:00:00Z Downloading packages...",
        "2024-01-01T00:00:00Z Analyzing project...",
        "  error • lib/main.dart:10:5 • Undefined name 'foo' • undefined_identifier",
        "2024-01-01T00:00:00Z 1 issue found.",
        "2024-01-01T00:00:00Z Post job cleanup.",
        "2024-01-01T00:00:00Z Saving cache...",
    ])
    filtered = _extract_issue_lines(log)
    assert "Undefined name 'foo'" in filtered
    assert "Resolving dependencies" not in filtered
    assert "Saving cache" not in filtered


def test_extract_issue_lines_keeps_context_after_test_failure():
    """flutter test FAILED 줄 뒤에 오는 Expected/Actual 어설션 diff는 그 줄 자체엔
    'error'/'warning' 키워드가 없어도 원인 파악에 필수라 문맥으로 같이 남아야 한다."""
    log = "\n".join([
        "00:03 +5 -1: widget test FAILED",
        "Expected: true",
        "  Actual: <false>",
        "2024-01-01T00:00:00Z Process completed with exit code 1.",
    ])
    filtered = _extract_issue_lines(log)
    assert "Expected: true" in filtered
    assert "Actual: <false>" in filtered


def test_extract_issue_lines_empty_when_no_match():
    assert _extract_issue_lines("all good, nothing to see here\nstill fine") == ""


def test_extract_issue_lines_caps_length_for_very_noisy_logs():
    noisy = "\n".join(f"error • lib/f{i}.dart:1:1 • something wrong • rule_{i}" for i in range(500))
    filtered = _extract_issue_lines(noisy)
    assert len(filtered) <= 3000
