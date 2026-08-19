"""
Implement가 만든 코드를 결정적으로(OpenHands 프롬프트 지시가 아니라 이 코드로)
빌드하고, QA가 재빌드 없이 그대로 쓸 수 있는 고정 경로에 남기는 로직만 분리한
모듈. run.py는 openhands.sdk(무거운 런타임 의존성)를 임포트 시점에 로드하기 때문에
이 로직을 run.py 안에 그대로 두면 테스트가 SDK 설치를 요구하게 된다 —
git_workspace.py/prompt_helpers.py와 같은 이유로 별도 모듈로 뽑아서 의존성 없이
테스트할 수 있게 한다.
"""
import os
import re
import shutil

from git_workspace import run

ARTIFACT_APK_NAME = "app-debug.apk"

# agents/autotest_ci/run.py, agents/qa_testlab/run.py의 동명 함수와 완전히
# 동일한 로직 — 각 에이전트가 독립된 Docker 빌드 컨텍스트(자기 파일만 COPY)라
# 공유 모듈 대신 그대로 복제한다. 로그 끝만 자르면 실제 원인이 cleanup 출력
# 뒤에 묻혀 잘려나가는 문제가 있어서, error/warning/실패 줄과 문맥만 남긴다.
_ISSUE_LINE_RE = re.compile(r"error|warning|exception|failed", re.IGNORECASE)
_CONTEXT_LINES_AFTER = 2
_MAX_FILTERED_CHARS = 3000


def _extract_issue_lines(log_text: str) -> str:
    lines = log_text.splitlines()
    keep = [False] * len(lines)
    for i, line in enumerate(lines):
        if _ISSUE_LINE_RE.search(line):
            for j in range(i, min(i + 1 + _CONTEXT_LINES_AFTER, len(lines))):
                keep[j] = True
    filtered = "\n".join(line for line, k in zip(lines, keep) if k)
    return filtered[-_MAX_FILTERED_CHARS:]


# flutter analyze 사람이 읽는 기본 출력 형식은 "  error • 메시지 • 파일:줄:칸 • 규칙"
# 처럼 줄 앞에 severity 토큰이 붙는다. Implement 자동 수정 루프는 "에러만" 잡고
# 워닝은 무시하도록(사용자 확인) 이 토큰으로 정확히 구분한다 — _extract_issue_lines의
# 넓은 error|warning|exception|failed 매칭은 피드백 텍스트를 만들 때만 쓰고,
# 루프를 계속할지 판단하는 건 이 함수로 한다.
_ANALYZE_ERROR_LINE_RE = re.compile(r"^\s*error\s*•", re.MULTILINE)


def _has_analyze_errors(analyze_output: str) -> bool:
    return bool(_ANALYZE_ERROR_LINE_RE.search(analyze_output))


def _cap_gradle_memory(workspace: str):
    """QA(agents/qa_testlab/run.py의 동명 함수)와 동일한 이유로 여기서도 필요하다 —
    다른 프로젝트의 QA/Implement 컨테이너가 동시에 Gradle 데몬을 띄우면 Docker
    Desktop VM(7.75GiB) 예산을 넘어 AAPT2가 시작도 못 하고 죽는다. 데몬을 끄고
    힙을 낮춰 매 빌드가 독립 프로세스로 안전하게 끝나게 한다."""
    gradle_dir = f"{workspace}/android"
    if not os.path.isdir(gradle_dir):
        return
    props_path = f"{gradle_dir}/gradle.properties"
    overrides = {
        "org.gradle.daemon": "false",
        "org.gradle.jvmargs": "-Xmx1536m",
        "org.gradle.parallel": "false",
    }
    lines = []
    if os.path.exists(props_path):
        with open(props_path, errors="replace") as f:
            lines = [ln for ln in f.read().splitlines() if ln.split("=", 1)[0].strip() not in overrides]
    lines += [f"{k}={v}" for k, v in overrides.items()]
    with open(props_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def build_and_handoff_apk(workspace: str, project_id: str, artifacts_root: str = "/workspace") -> tuple[bool, str, str | None]:
    """Implement가 실제로 만든 코드를 결정적으로 빌드하고, git 워크스페이스 밖의
    고정 경로에 복사해둔다.

    예전엔 OpenHands에게 "빌드까지 확인하라"고 프롬프트로만 지시했는데, 그 빌드
    결과물은 OpenHands 컨테이너 세션이 끝나면 버려지고, QA가 같은 소스로 처음부터
    다시 빌드했다 — (1) 빌드를 두 번 해서 느리고, (2) 엄밀히는 QA가 "Implement가
    만든 그 바이너리"가 아니라 "QA가 새로 빌드한 바이너리"를 테스트하는 셈이었다.
    여기서 한 번 더 빌드해 고정 경로(app-debug.apk)에 남기면, QA는 재빌드 없이
    이 파일을 그대로 Firebase Test Lab에 제출할 수 있다 — 단, qa flavor처럼 특수
    빌드 커맨드가 필요한 프로젝트는 QA가 기존대로 자기 빌드를 쓰게
    남겨둔다(qa_testlab/run.py의 process_task에서 판단).

    artifacts_root는 프로덕션에서는 항상 "/workspace"(shared-workspace 볼륨)지만,
    테스트에서 실제 호스트 경로에 쓰지 않도록 오버라이드할 수 있게 파라미터로 뺐다.

    반환: (성공 여부, 실패 시 에러 요약, 성공 시 handoff APK 경로)."""
    if not os.path.exists(f"{workspace}/pubspec.yaml"):
        return False, "pubspec.yaml이 없음 — Flutter 프로젝트가 아직 생성되지 않음", None

    _cap_gradle_memory(workspace)
    pubget = run(["flutter", "pub", "get"], cwd=workspace, timeout=180)
    if pubget.returncode != 0:
        return False, f"flutter pub get 실패: {pubget.stderr[-500:]}", None

    build = run(["flutter", "build", "apk", "--debug"], cwd=workspace, timeout=600)
    if build.returncode != 0:
        detail = _extract_issue_lines(f"{build.stdout}\n{build.stderr}") or \
            f"{build.stdout[-800:]}\n{build.stderr[-800:]}"
        return False, f"flutter build apk 실패:\n{detail}", None

    found = run(["find", f"{workspace}/build/app/outputs/flutter-apk", "-name", "*.apk"], cwd=workspace, timeout=10)
    apk_candidates = found.stdout.strip().splitlines()
    if not apk_candidates:
        return False, "빌드는 성공했지만 build/app/outputs/flutter-apk/ 안에서 APK를 찾지 못함", None

    artifacts_dir = f"{artifacts_root}/{project_id}-artifacts"
    os.makedirs(artifacts_dir, exist_ok=True)
    handoff_path = f"{artifacts_dir}/{ARTIFACT_APK_NAME}"
    shutil.copyfile(apk_candidates[0], handoff_path)
    return True, "", handoff_path
