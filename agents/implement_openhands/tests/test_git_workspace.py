"""
ensure_git_workspace() 회귀 테스트.

실제로 발생했던 버그: 신규 프로젝트를 자동 생성한 레포에 pm/designer/architect가
먼저 산출물 파일을 써놓은 상태로 implement가 clone을 시도하면, 디렉토리가
비어있지 않아서 `git clone`이 "destination path already exists and is not an
empty directory"로 실패했다. 여기서는 실제 GitHub 대신 로컬 bare 레포를
origin으로 써서 네트워크 없이 같은 상황을 재현하고, 고친 로직이 이 상황을
정상적으로 우회하는지 검증한다.

실행: cd agents/implement_openhands && pytest tests/ -v
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from git_workspace import ensure_git_workspace, run


def make_fake_origin(tmp_path) -> str:
    """네트워크 없이 clone/fetch가 가능한 로컬 bare 레포를 만들어 origin으로 쓴다."""
    bare = str(tmp_path / "origin.git")
    seed = str(tmp_path / "seed")
    run(["git", "init", "--bare", "-b", "main", bare], cwd=str(tmp_path))
    run(["git", "init", "-b", "main", seed], cwd=str(tmp_path))
    with open(os.path.join(seed, "README.md"), "w") as f:
        f.write("# seed\n")
    run(["git", "config", "user.email", "test@test"], cwd=seed)
    run(["git", "config", "user.name", "test"], cwd=seed)
    run(["git", "add", "."], cwd=seed)
    run(["git", "commit", "-m", "seed"], cwd=seed)
    run(["git", "remote", "add", "origin", bare], cwd=seed)
    run(["git", "push", "origin", "main"], cwd=seed)
    return bare


def test_empty_workspace_uses_plain_clone(tmp_path):
    origin = make_fake_origin(tmp_path)
    workspace = str(tmp_path / "empty_ws")
    # ensure_git_workspace가 알아서 만들도록 디렉토리를 미리 만들지 않는다

    ok, detail = ensure_git_workspace(workspace, origin)
    assert ok, detail
    assert detail == "clone 성공"
    assert os.path.exists(f"{workspace}/.git")
    assert os.path.exists(f"{workspace}/README.md")


def test_regression_nonempty_workspace_does_not_use_plain_clone(tmp_path):
    """핵심 회귀 테스트: pm/design이 먼저 파일을 써놓은 워크스페이스(= .git은 없고
    일반 파일만 있음)에 대해, 실패하는 `git clone`을 시도하지 않고
    init+remote+fetch+checkout으로 우회해서 성공해야 한다."""
    origin = make_fake_origin(tmp_path)
    workspace = str(tmp_path / "dirty_ws")
    os.makedirs(workspace)
    # pm/design이 미리 써놓은 산출물 흉내
    with open(os.path.join(workspace, "pm_output.md"), "w") as f:
        f.write("요구사항 문서\n")

    # 대조군: 이 상황에서 naive plain clone은 실제로 실패한다는 것부터 확인
    naive = subprocess.run(
        ["git", "clone", origin, workspace],
        capture_output=True, text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    assert naive.returncode != 0
    assert "already exists" in naive.stderr or "not an empty directory" in naive.stderr

    ok, detail = ensure_git_workspace(workspace, origin)
    assert ok, detail
    assert "init 방식" in detail
    # 기존 산출물 파일이 보존됐는지
    assert os.path.exists(os.path.join(workspace, "pm_output.md"))
    # 레포와 정상적으로 연결됐는지
    assert os.path.exists(os.path.join(workspace, "README.md"))
    assert os.path.exists(os.path.join(workspace, ".git"))


def test_already_initialized_workspace_is_left_untouched(tmp_path):
    """이미 .git이 있으면 (오케스트레이터가 clone을 이미 해둔 정상 경로) 아무것도
    다시 하지 않아야 한다."""
    origin = make_fake_origin(tmp_path)
    workspace = str(tmp_path / "ready_ws")
    run(["git", "clone", origin, workspace], cwd=str(tmp_path))

    ok, detail = ensure_git_workspace(workspace, "unused/repo")
    assert ok
    assert detail == "이미 git 저장소"


def test_invalid_repo_reports_failure_without_raising(tmp_path):
    """origin 자체가 없는 경우에도 예외 없이 (False, 사유) 로 실패를 보고해야
    상위 호출부(process_task)가 안전하게 사용자에게 에러 메시지를 보여줄 수 있다."""
    workspace = str(tmp_path / "broken_ws")
    os.makedirs(workspace)
    with open(os.path.join(workspace, "some_file.txt"), "w") as f:
        f.write("x")

    ok, detail = ensure_git_workspace(workspace, "/no/such/origin")
    assert ok is False
    assert detail
