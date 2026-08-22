"""
워크스페이스-레포 연결 로직만 따로 뺀 모듈.

run.py는 openhands SDK/redis/httpx 등 무거운 의존성을 임포트 시점에 로드하고
`/workspace/logs`에 파일을 쓰기 때문에 이 로직만 골라서 단위 테스트하기 어렵다.
git 연결 로직 자체는 subprocess만 쓰는 순수 로직이라 여기 분리해서 의존성 없이
테스트할 수 있게 한다.
"""
import os
import re
import subprocess


def run(cmd: list[str], cwd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


_CONFLICT_START_RE = re.compile(r"^<{7} ", re.MULTILINE)
_CONFLICT_MID_RE   = re.compile(r"^={7}$", re.MULTILINE)
_CONFLICT_END_RE   = re.compile(r"^>{7} ", re.MULTILINE)


def find_conflict_marker_files(workspace: str, porcelain_status: str) -> list[str]:
    """OpenHands는 "git commit/push는 직접 하지 마세요"라는 지시만 받을 뿐
    git merge/pull 자체를 막을 방법은 없다 — TerminalTool로 스스로 그런 명령을
    실행하다 실제 충돌이 나면(2026-08-22 recoveryfit에서 실제 발생: 이전 라운드
    커밋과 충돌해 .github/workflows/validation.yml에 <<<<<<< HEAD 마커가 그대로
    남았고, run.py의 무조건 commit 코드가 그걸 그대로 커밋/푸시해서 YAML이
    깨지고 CI가 매번 파싱 단계에서 즉시 실패했다) run.py가 commit하기 직전에
    이 함수로 걸러서 커밋 자체를 막는다. git이 이미 unmerged로 표시한 파일(U)
    뿐 아니라, 변경된 파일들의 실제 내용에 컨플릭트 마커 3종(시작/구분선/끝)이
    모두 있는 경우까지 같이 잡는다 — `git add`를 먼저 하면 상태가 'staged'로
    넘어가 U 표시가 사라질 수 있어서, 내용 스캔이 없으면 그 경우를 놓친다."""
    unmerged = {
        line[3:].strip() for line in porcelain_status.splitlines()
        if "U" in line[:2] or line[:2] in ("AA", "DD")
    }
    marker_files = set()
    for line in porcelain_status.splitlines():
        path = line[3:].strip()
        if not path or path in unmerged:
            continue
        full_path = os.path.join(workspace, path)
        if not os.path.isfile(full_path):
            continue
        try:
            with open(full_path, "r", errors="ignore") as f:
                content = f.read(200_000)
        except OSError:
            continue
        if _CONFLICT_START_RE.search(content) and _CONFLICT_MID_RE.search(content) and _CONFLICT_END_RE.search(content):
            marker_files.add(path)
    return sorted(unmerged | marker_files)


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
