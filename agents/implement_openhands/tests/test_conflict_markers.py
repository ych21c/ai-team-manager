"""
find_conflict_marker_files() 회귀 테스트.

실제로 발생했던 버그: OpenHands(TerminalTool로 셸 접근 가능)가 스스로 git
merge/pull을 실행하다 실제 충돌이 나면, run.py의 "무조건 git add -A && git
commit" 코드가 컨플릭트 마커(<<<<<<< / ======= / >>>>>>>)가 남은 파일을 그대로
커밋/푸시했다. 2026-08-22 recoveryfit(ych21c/recoveryfit)에서 이게
.github/workflows/validation.yml에 실제로 발생해 YAML이 깨지고 CI가 매번
파싱 단계에서 즉시 실패(0 jobs)하는 사고로 이어졌다.

실행: cd agents/implement_openhands && pytest tests/test_conflict_markers.py -v
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from git_workspace import run, find_conflict_marker_files


def test_no_conflicts_returns_empty():
    porcelain = " M lib/main.dart\n?? lib/new_file.dart\n"
    assert find_conflict_marker_files("/nonexistent", porcelain) == []


def test_git_reported_unmerged_path_is_flagged(tmp_path):
    porcelain = "UU .github/workflows/validation.yml\n"
    assert find_conflict_marker_files(str(tmp_path), porcelain) == [
        ".github/workflows/validation.yml"
    ]


def test_regression_real_merge_conflict_is_detected(tmp_path):
    """OpenHands가 실제로 밟았을 시나리오를 그대로 재현한다: 두 브랜치가
    같은 파일의 같은 줄을 다르게 고쳐서 실제 `git merge`가 컨플릭트로
    끝나고, 워크스페이스에 마커가 남은 상태를 만든 뒤 그걸 잡아내는지 검증."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    run(["git", "init", "-b", "main", repo], cwd=str(tmp_path))
    run(["git", "config", "user.email", "test@test"], cwd=repo)
    run(["git", "config", "user.name", "test"], cwd=repo)

    workflow_path = os.path.join(repo, "validation.yml")
    with open(workflow_path, "w") as f:
        f.write("steps:\n  - run: flutter test\n")
    run(["git", "add", "."], cwd=repo)
    run(["git", "commit", "-m", "base"], cwd=repo)

    run(["git", "checkout", "-b", "incoming"], cwd=repo)
    with open(workflow_path, "w") as f:
        f.write("steps:\n  - run: flutter test --incoming\n")
    run(["git", "add", "."], cwd=repo)
    run(["git", "commit", "-m", "AI Implement: add apk build step"], cwd=repo)

    run(["git", "checkout", "main"], cwd=repo)
    with open(workflow_path, "w") as f:
        f.write("steps:\n  - run: flutter test --main\n")
    run(["git", "add", "."], cwd=repo)
    run(["git", "commit", "-m", "base 2"], cwd=repo)

    merge = run(["git", "merge", "incoming", "--no-edit"], cwd=repo)
    assert merge.returncode != 0, "이 테스트는 실제 충돌이 나야 유효하다"

    porcelain = run(["git", "status", "--porcelain"], cwd=repo).stdout
    found = find_conflict_marker_files(repo, porcelain)

    assert found == ["validation.yml"]
    with open(workflow_path) as f:
        content = f.read()
    assert "<<<<<<< HEAD" in content
