"""
회귀 테스트 — _extract_issue_lines. agents/autotest_ci/test_run.py와 동일한
케이스를 그대로 복제(_extract_issue_lines 자체가 agents/autotest_ci/run.py의
동명 함수를 복제한 것이므로 — 각 에이전트가 독립된 Docker 빌드 컨텍스트라
공유 모듈 대신 로직/테스트 둘 다 복제하는 게 이 repo의 기존 패턴).

실행: 호스트 python(3.9)은 run.py의 `float | None` 문법(3.10+)을 못 읽어서
바로 못 돌린다 — agents/qa_testlab 이미지 컨테이너 안에서 실행해야 한다:
  docker run --rm -v "$(pwd)/agents/qa_testlab:/app" -w /app \
    ai-dev-team-agent-qa:latest sh -c "pip install -q pytest; python -m pytest test_run.py -v"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run import _extract_issue_lines


def test_extract_issue_lines_keeps_analyzer_error_drops_noise():
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


def test_extract_issue_lines_recovers_stdout_when_stderr_alone_exceeds_naive_cap():
    """구버전 버그 회귀 방지: stdout/stderr를 각각 800자로 자른 뒤 다시 합쳐서
    1000자로 재슬라이스하면, stderr만으로 1000자를 넘길 때 stdout 쪽 에러가
    통째로 사라졌다. 필터는 원본 전체를 받아 실제 에러 줄을 양쪽에서 찾아야 한다."""
    stdout = "error • lib/main.dart:1:1 • stdout 쪽 진짜 원인 • some_rule\n"
    stderr = "noise line\n" * 200  # 1000자보다 훨씬 긴 stderr
    combined = f"{stdout}\n{stderr}"
    filtered = _extract_issue_lines(combined)
    assert "stdout 쪽 진짜 원인" in filtered
