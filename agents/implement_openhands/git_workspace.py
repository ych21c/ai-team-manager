"""
워크스페이스-레포 연결 로직만 따로 뺀 모듈.

run.py는 openhands SDK/redis/httpx 등 무거운 의존성을 임포트 시점에 로드하고
`/workspace/logs`에 파일을 쓰기 때문에 이 로직만 골라서 단위 테스트하기 어렵다.
git 연결 로직 자체는 subprocess만 쓰는 순수 로직이라 여기 분리해서 의존성 없이
테스트할 수 있게 한다.
"""
import os
import subprocess


def run(cmd: list[str], cwd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def ensure_git_workspace(workspace: str, repo_url: str) -> tuple[bool, str]:
    """워크스페이스를 레포와 연결한다 (오케스트레이터가 아직 clone 안 했을 때의 방어선).

    실제로 발생했던 버그: pm/designer/architect가 이미 이 워크스페이스에 산출물
    파일을 써놓은 상태에서 여기 도달하면, 디렉토리가 비어있지 않아서
    `git clone <url> <workspace>`가 "destination path already exists and is
    not an empty directory"로 실패했다. 디렉토리가 비어있을 때만 clone을 쓰고,
    이미 파일이 있으면 clone 대신 init+remote+fetch+checkout으로 우회해서
    기존 파일을 지우지 않으면서 레포에 연결한다."""
    if os.path.exists(f"{workspace}/.git"):
        return True, "이미 git 저장소"

    has_existing_files = os.path.isdir(workspace) and len(os.listdir(workspace)) > 0

    if not has_existing_files:
        cp = run(["git", "clone", repo_url, workspace], cwd="/", timeout=180)
        if cp.returncode == 0:
            return True, "clone 성공"
        return False, cp.stderr[:300]

    os.makedirs(workspace, exist_ok=True)
    steps = [
        ["git", "init"],
        ["git", "remote", "add", "origin", repo_url],
        ["git", "fetch", "origin"],
        ["git", "checkout", "-B", "main", "origin/main"],
    ]
    for cmd in steps:
        cp = run(cmd, cwd=workspace, timeout=120)
        if cp.returncode != 0:
            return False, f"{' '.join(cmd)} 실패: {cp.stderr[:300]}"
    return True, "기존 파일을 보존하며 init 방식으로 레포에 연결"
