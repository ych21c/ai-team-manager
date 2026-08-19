"""
QA Agent — Firebase Test Lab 기반 실제 무작위(Robo) 테스트.

텍스트로 리뷰만 하던 기존 QA와 달리, Implement가 만든 브랜치에서 실제로 APK를
빌드하고 Firebase Test Lab에 제출해 Robo(구글의 자동 탐색적/무작위 테스트)
테스트를 돌린다. 결과 영상을 받아와 워크스페이스에 저장하면 오케스트레이터가
웹 UI에서 볼 수 있게 서빙한다.

프로젝트마다 다른 Android 빌드 관례에 종속되지 않는다 — `determine_build_command()`가
`android/app/build.gradle.kts`에 `qa` flavor가 정의돼 있고 `lib/main_test.dart`
진입점도 있을 때만 `--flavor qa`로 빌드하고, 둘 중 하나라도 없으면(대부분의 새
프로젝트) 그냥 평범한 `flutter build apk --debug`로 자동 대체한다. 어떤 경우든
`find_apk()`가 `build/app/outputs/flutter-apk/` 아래에서 flavor 이름과 무관하게
APK를 찾으므로 새 프로젝트를 만들 때 이 부분을 따로 설정할 필요가 없다.

FIREBASE_TEST_PROJECT(기본값 "goodenough-test")는 Test Lab을 실행하는 GCP
프로젝트일 뿐, 테스트 대상 앱이 Firebase를 쓰는지와는 무관한 배포 단위 설정이라
프로젝트별로 바꿀 필요가 없다.
"""
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
import uuid

import anthropic
import redis.asyncio as aioredis

REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")
AGENT_NAME   = "qa"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
QA_MODEL = "claude-haiku-4-5-20251001"
# agents/implement_openhands/run.py의 ARTIFACT_APK_NAME과 반드시 같은 값이어야
# Implement가 남긴 handoff APK를 QA가 찾을 수 있다.
IMPLEMENT_ARTIFACT_APK_NAME = "app-debug.apk"
# USD / 1M 토큰(agents/base/agent.py의 표와 동일 — 모델 바뀌면 같이 갱신).
_TOKEN_PRICE_PER_MTOK = {QA_MODEL: (1.0, 5.0)}

def _cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = _TOKEN_PRICE_PER_MTOK.get(model)
    if not price:
        return None
    in_rate, out_rate = price
    return round(input_tokens * in_rate / 1_000_000 + output_tokens * out_rate / 1_000_000, 6)

# 한 QA 태스크(process_task 한 번) 안에서 verify_scenarios/design_qa_check가 각각
# 여러 번 Claude를 호출할 수 있어, 태스크 단위로 누적한다. asyncio 단일 태스크가
# 끝까지 끝난 뒤에야 다음 태스크를 처리하므로(await 지점에서 교차 실행되긴 하지만
# 이 프로세스는 태스크를 하나씩 순차 처리) 전역 카운터로도 안전 — process_task
# 시작에서 리셋하고 끝에서 읽는다.
_token_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

def _track_usage(resp):
    _token_usage["input"]  += resp.usage.input_tokens
    _token_usage["output"] += resp.usage.output_tokens
    # verify_scenarios가 PM/Design/목업/규칙을 system 블록으로 캐싱하기 시작한
    # 뒤로, cache_read가 계속 0이면 캐싱이 실제로 안 걸리고 있다는 신호라
    # 플로우차트 탭에서 바로 보이게 같이 누적해둔다.
    _token_usage["cache_read"]  += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
    _token_usage["cache_write"] += getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
GCLOUD_KEY_FILE      = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
FIREBASE_TEST_PROJECT = os.getenv("FIREBASE_TEST_PROJECT", "goodenough-test")
TEST_DEVICE  = os.getenv("TEST_LAB_DEVICE", "model=MediumPhone.arm,version=34,locale=en,orientation=portrait")
TEST_TIMEOUT = os.getenv("TEST_LAB_TIMEOUT", "3m")
# Test Lab의 자동 생성 기본 버킷은 storage.objects.create 권한이 프로젝트 IAM으로
# 해결되지 않는 경우가 있었다 (라이브 테스트로 확인, 프로젝트 owner도 접근 불가).
# 직접 만든 버킷을 명시적으로 지정해 이 문제를 피한다.
TEST_LAB_RESULTS_BUCKET = os.getenv("TEST_LAB_RESULTS_BUCKET", "")

STREAM_INBOX  = f"agent:{AGENT_NAME}:inbox"
STREAM_EVENTS = "orchestrator:events"


class _Tee:
    """stdout/stderr를 컨테이너 로그와 /workspace/logs 파일에 동시에 남긴다
    (나중에 문제 분석용 — 이 에이전트는 상시 싱글턴이라 재시작돼도 이력이 남아야 함)."""
    def __init__(self, stream, path: str):
        self._stream = stream
        self._file = open(path, "a", buffering=1)

    def write(self, data):
        self._stream.write(data)
        self._file.write(data)

    def flush(self):
        self._stream.flush()
        self._file.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _setup_file_log(name: str):
    os.makedirs("/workspace/logs", exist_ok=True)
    path = f"/workspace/logs/{name}.log"
    if os.path.exists(path) and os.path.getsize(path) > 5_000_000:
        os.replace(path, path + ".1")
    sys.stdout = _Tee(sys.stdout, path)
    sys.stderr = _Tee(sys.stderr, path)


_setup_file_log(AGENT_NAME)

_GCLOUD_READY = False


def run(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


async def emit(r: aioredis.Redis, event: dict):
    await r.xadd(STREAM_EVENTS, {"payload": json.dumps(event)})


_ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[a-zA-Z]")


async def _flush_build_progress(r: aioredis.Redis, project_id: str, label: str, buf: bytearray, elapsed: int):
    """buf가 비어 있어도(=flutter가 오랫동안 아무 것도 안 찍는 구간) 반드시
    호출한다 — "아직 살아있고 몇 초째 진행 중"이라는 신호 자체가 로그가 통째로
    없는 것보다 훨씬 중요하다. flutter build는 Gradle을 -q(조용히) 모드로 감싸서
    실제로 몇 분씩 stdout이 전혀 안 나오는 구간이 있다(counter-app에서 확인됨)."""
    text = _ANSI_RE.sub(b"", bytes(buf)).decode(errors="replace")
    # flutter/gradle는 스피너를 \r로 그리는 줄이 많아서, 그런 중간 프레임 말고
    # 실제 내용이 있는 줄만 모아서 보여준다.
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]
    snippet = "\n".join(lines)[-1200:]
    body = f"\n{snippet}" if snippet else " (아직 출력 없음 — Gradle이 조용히 작업 중)"
    await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                    "content": f"⏳ {label} 진행 중... ({elapsed}초 경과){body}"})


class _StreamedResult:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


async def run_streaming(cmd: list[str], cwd: str, timeout: int, r: aioredis.Redis,
                         project_id: str, label: str, interval: float = 4.0) -> _StreamedResult:
    """flutter build처럼 몇 분씩 걸리는 명령을 돌리는 동안, 끝날 때까지 QA
    항목에 아무 신호가 없어서 멈춘 것처럼 보이던 문제의 수정 — 몇 초 간격으로
    지금까지 나온 출력을 웹 UI 로그(message 이벤트)로 흘려보낸다."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    pending = bytearray()
    all_output = bytearray()
    start = time.monotonic()
    last_flush = start
    deadline = start + timeout

    async def pump():
        nonlocal last_flush
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            # read()가 오래 블록돼도(=flutter가 조용한 구간) interval마다는
            # 반드시 하트비트를 내보내야 하므로, 읽기 자체에 짧은 타임아웃을 건다.
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=min(interval, remaining))
            except asyncio.TimeoutError:
                chunk = None
            else:
                if chunk == b"":
                    break  # EOF — 프로세스 종료
                if chunk:
                    pending.extend(chunk)
                    all_output.extend(chunk)
            now = time.monotonic()
            if now - last_flush >= interval:
                await _flush_build_progress(r, project_id, label, pending, int(now - start))
                pending.clear()
                last_flush = now

    try:
        await pump()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    await proc.wait()
    await _flush_build_progress(r, project_id, label, pending, int(time.monotonic() - start))

    return _StreamedResult(proc.returncode, all_output.decode(errors="replace"))


def ensure_gcloud_auth() -> bool:
    global _GCLOUD_READY
    if _GCLOUD_READY:
        return True
    if not GCLOUD_KEY_FILE or not os.path.exists(GCLOUD_KEY_FILE):
        return False
    cp = run(["gcloud", "auth", "activate-service-account", f"--key-file={GCLOUD_KEY_FILE}"], timeout=30)
    if cp.returncode != 0:
        print(f"[qa] gcloud 인증 실패: {cp.stderr[:300]}")
        return False
    run(["gcloud", "config", "set", "project", FIREBASE_TEST_PROJECT], timeout=15)
    _GCLOUD_READY = True
    return True


def find_apk(workspace: str) -> str | None:
    candidates = run(
        ["find", f"{workspace}/build/app/outputs/flutter-apk", "-name", "*.apk"],
        timeout=10,
    ).stdout.strip().splitlines()
    return candidates[0] if candidates else None


def parse_gcloud_output(stdout: str, stderr: str) -> dict:
    """gcloud firebase test android run의 사람이 읽는 출력에서 결과 요약과
    GCS 결과 경로를 뽑아낸다 (JSON 스키마보다 이 텍스트 포맷이 더 안정적).

    "Raw results will be stored in your GCS bucket at [...]" 줄은 gs:// URI가
    아니라 https://console.developers.google.com/storage/browser/<bucket>/<path>/
    형태의 콘솔 링크로 나온다 (실제 라이브 테스트로 확인됨) — gs://로 변환해서 사용한다.
    """
    # gcloud가 컬러 코드를 "\x1b[32mPassed \x1b[39;0m"처럼 단어에 바로 붙여 출력해서
    # ANSI 이스케이프를 먼저 제거하지 않으면 \bPassed\b가 단어 경계를 못 잡는다
    # (실제 라이브 테스트로 확인됨).
    combined = re.sub(r"\x1b\[[0-9;]*m", "", stdout + "\n" + stderr)

    gcs_path = None
    console_match = re.search(r"https://console\.developers\.google\.com/storage/browser/([^\]\s]+)", combined)
    if console_match:
        gcs_path = f"gs://{console_match.group(1)}"
    else:
        gcs_match = re.search(r"(gs://[^\s\]]+)", combined)
        gcs_path = gcs_match.group(1) if gcs_match else None

    passed = bool(re.search(r"\bPassed\b", combined)) and not re.search(r"\bFailed\b|\bCrashed\b", combined)
    outcome_lines = [l.strip() for l in combined.splitlines() if re.search(r"Passed|Failed|Crashed|Inconclusive", l)]
    return {
        "passed": passed,
        "gcs_path": gcs_path,
        "summary": " | ".join(outcome_lines[:5]) if outcome_lines else combined[-500:],
    }


def _finalize_qa_outputs(result: dict, video_ok: bool, manual_count: int,
                          design_mismatch_feedback: str | None,
                          token_usage: dict | None = None) -> dict:
    """기능(빌드+Robo)은 통과해도 디자인이 스펙과 다르면 needs_rework로 뒤집어서,
    QA가 판단만 하고 끝내지 않고 기존 QA 재작업 루프(_retry_implement_with_feedback,
    MAX_QA_RETRIES로 무한 루프 방지됨)를 그대로 타게 한다 — 예전엔 메시지만
    남기고 아무 조치가 없어서 디자인 불일치가 방치됐었다(counter-app에서 실제로
    발생 — 배경색/AppBar/버튼이 전부 스펙과 달랐는데도 아무도 안 고쳤다)."""
    outputs = {
        "agent": AGENT_NAME,
        "passed": result["passed"],
        "summary": result["summary"],
        "video_available": video_ok,
        "manual_review_count": manual_count,
    }
    if design_mismatch_feedback:
        outputs["passed"] = False
        outputs["needs_rework"] = True
        outputs["feedback"] = design_mismatch_feedback
        outputs["summary"] = "디자인 QA 불일치"
    if token_usage:
        outputs["input_tokens"]  = token_usage["input"]
        outputs["output_tokens"] = token_usage["output"]
        outputs["cache_read_tokens"]  = token_usage.get("cache_read", 0)
        outputs["cache_write_tokens"] = token_usage.get("cache_write", 0)
        cost = _cost_usd(QA_MODEL, token_usage["input"], token_usage["output"])
        if cost is not None:
            outputs["cost_usd"] = cost
    return outputs


def determine_build_command(workspace: str) -> tuple[list[str] | None, str | None]:
    """이 프로젝트가 실제로 무엇으로 빌드 가능한지 확인해서 build 커맨드를 정한다.
    예전 child-care-medication 컨벤션(qa flavor + main_test.dart)을 모든 프로젝트에
    강제하지 않고, 있으면 쓰고 없으면 일반 디버그 빌드로 자연스럽게 대체한다.
    반환: (build_cmd, None) 또는 (None, 재작업이_필요한_이유)."""
    if not os.path.exists(f"{workspace}/pubspec.yaml"):
        return None, "pubspec.yaml이 없습니다 — 유효한 Flutter 프로젝트가 아닙니다. `flutter create`로 프로젝트 골격부터 만드세요."

    has_qa_flavor = False
    gradle_path = f"{workspace}/android/app/build.gradle.kts"
    if os.path.exists(gradle_path):
        with open(gradle_path, errors="replace") as f:
            has_qa_flavor = '"qa"' in f.read()
    has_test_entry = os.path.exists(f"{workspace}/lib/main_test.dart")

    if has_qa_flavor and has_test_entry:
        return ["flutter", "build", "apk", "--flavor", "qa", "--debug", "-t", "lib/main_test.dart"], None
    return ["flutter", "build", "apk", "--debug"], None


def _cap_gradle_memory(workspace: str):
    """Docker Desktop VM 전체 메모리가 7.75GiB뿐인데 Gradle 데몬 기본 힙 설정은
    -Xmx8G라 그것만으로 VM 예산을 넘는다 — implement 컨테이너(수 GiB)와 동시에
    떠 있으면 AAPT2 데몬이 시작도 못 하고 죽는다("Daemon startup failed",
    빌드가 1~2초 만에 실패). 실제로 이 때문에 정상 코드가 반복 재작업 요청으로
    잘못 튕겨나간 사고가 있었다 — 코드가 아니라 여기 설정이 원인이었다.
    데몬을 아예 끄고(빌드 1회성이라 데몬 재사용 이득이 없음) 힙을 이 컨테이너
    예산에 맞게 낮춰서 매 빌드가 독립된 프로세스로 안전하게 끝나게 한다."""
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


# agents/autotest_ci/run.py의 동명 함수와 완전히 동일한 로직 — 각 에이전트가
# 독립된 Docker 빌드 컨텍스트(자기 run.py만 COPY)라 공유 모듈 대신 그대로
# 복제한다(_cap_gradle_memory와 같은 이유). Gradle/flutter build 실패 로그를
# stdout/stderr 끝만 자르면(예전 방식) 실제 원인이 cleanup 출력 뒤에 묻혀
# 잘려나가는 문제가 있었다 — error/warning/실패 줄과 문맥만 남긴다.
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


def build_instrumentation_apks(workspace: str) -> tuple[bool, str, str | None, str | None]:
    """SCENARIO_TEST_FILE(integration_test/scenario_test.dart)을 로컬 시뮬레이션
    (flutter test)뿐 아니라 실제 기기(Firebase Test Lab)에서도 돌리기 위한 앱/테스트
    APK 두 개를 빌드한다. `flutter build apk`는 androidTest APK를 만들어주지
    않으므로, Flutter 공식 문서가 안내하는 대로 gradle을 직접 호출한다:
    - `assembleAndroidTest`로 계측(instrumentation) 러너 APK를,
    - `-Ptarget=`으로 진입점을 시나리오 테스트 파일로 지정한 `assembleDebug`로
      "실행하면 앱이 아니라 이 테스트를 도는" 앱 APK를 만든다.
    이 두 APK를 함께 제출해야 `gcloud firebase test android run --type instrumentation`이
    동작한다(Robo용 일반 app-debug.apk와는 진입점이 다른 별개의 빌드).

    빌드 자체가 실패하면(Android 프로젝트 구조/의존성 문제 등, 앱 로직과 무관한
    인프라성 이슈일 수 있음) needs_rework로 몰아 재작업 루프를 낭비시키지 않도록,
    process_task에서 이 실패는 차단이 아니라 보고만 하고 넘어가게 한다.

    반환: (성공 여부, 실패 시 에러 요약, 성공 시 app apk 경로, 성공 시 test apk 경로)."""
    android_dir = f"{workspace}/android"
    if not os.path.isdir(android_dir):
        return False, "android/ 디렉토리가 없음", None, None

    test_apk_build = run(["./gradlew", "app:assembleAndroidTest"], cwd=android_dir, timeout=600)
    if test_apk_build.returncode != 0:
        detail = _extract_issue_lines(f"{test_apk_build.stdout}\n{test_apk_build.stderr}") or \
            f"{test_apk_build.stdout[-800:]}\n{test_apk_build.stderr[-800:]}"
        return False, f"androidTest APK 빌드 실패:\n{detail}", None, None

    target_apk_build = run(
        ["./gradlew", "app:assembleDebug", f"-Ptarget={workspace}/{SCENARIO_TEST_FILE}"],
        cwd=android_dir, timeout=600,
    )
    if target_apk_build.returncode != 0:
        detail = _extract_issue_lines(f"{target_apk_build.stdout}\n{target_apk_build.stderr}") or \
            f"{target_apk_build.stdout[-800:]}\n{target_apk_build.stderr[-800:]}"
        return False, f"시나리오 진입점 앱 APK 빌드 실패:\n{detail}", None, None

    app_apk = f"{workspace}/build/app/outputs/apk/debug/app-debug.apk"
    test_apk = f"{workspace}/build/app/outputs/apk/androidTest/debug/app-debug-androidTest.apk"
    if not os.path.exists(app_apk) or not os.path.exists(test_apk):
        return False, "빌드는 성공했지만 예상 경로에서 APK를 찾지 못함", None, None
    return True, "", app_apk, test_apk


# 파일 하나하나를 잘게 자르던 예전 방식(SOURCE_EXCERPT_PER_FILE_LIMIT=2000,
# 그다음 6000)은 두 번이나 같은 사고를 냈다 — counter_screen.dart(9KB)의 버튼
# 위젯 코드가 파일 뒷부분에 있었는데 앞부분만 잘려 들어가서, QA 시나리오 생성
# LLM이 실제로 있는 FloatingActionButton을 못 보고 "ElevatedButton"으로
# 지어내 매 라운드 다른 위젯 타입을 기대하는 테스트를 써서 계속 실패했다
# (counter-app에서 재현). Claude Haiku의 실제 컨텍스트 윈도우(200K 토큰 ≈
# 800K자)에 비하면 손으로 짠 Flutter lib/ 소스는 몇 KB~수십 KB 수준이라
# "파일 하나하나를 작게 자르기"는 애초에 불필요한 방어였다 — 이제 각 파일은
# 항상 전체를 다 보여주고, 정말 비정상적인 경우(예: 실수로 커밋된 대용량
# 생성 파일)에 대한 안전장치로 전체 합계에만 훨씬 넉넉한 한도를 둔다.
SOURCE_TOTAL_EXCERPT_LIMIT = 120_000


def _list_source_files(workspace: str) -> str:
    """lib/(Flutter 실제 앱 코드) 파일 목록 + 각 파일 전체 내용을 모아 LLM
    검증용 컨텍스트로 만든다. 목록만으론 "빈 파일 껍데기만 만들고 끝"인지
    못 잡아내서 실제 내용도 같이 본다. 파일 단위로는 자르지 않는다 — 잘랐다가
    화면 파일 뒷부분의 실제 위젯 코드가 통째로 안 보여서 LLM이 없는 걸로
    오판하는 사고가 반복됐다. 전체 합계가 SOURCE_TOTAL_EXCERPT_LIMIT을
    넘는 비정상적인 경우에만 끝부분을 자르고 그렇다고 명시한다."""
    lib_dir = f"{workspace}/lib"
    if not os.path.isdir(lib_dir):
        return "(lib/ 디렉토리 자체가 없음 — Flutter 앱 코드가 전혀 없는 상태)"
    lines = []
    for root, _, files in os.walk(lib_dir):
        for fname in sorted(files):
            if not fname.endswith(".dart"):
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, workspace)
            try:
                with open(path, errors="replace") as f:
                    content = f.read()
            except OSError:
                content = ""
            lines.append(f"### {rel} ({len(content)}자)\n{content}")
    result = "\n\n".join(lines) if lines else "(lib/ 안에 .dart 파일이 없음)"
    if len(result) > SOURCE_TOTAL_EXCERPT_LIMIT:
        result = (
            result[:SOURCE_TOTAL_EXCERPT_LIMIT]
            + "\n... (이하 생략 — 전체 소스가 비정상적으로 커서 잘림, 이 잘림만으로 미구현 판정하지 말 것)"
        )
    return result


MOCKUP_TOTAL_EXCERPT_LIMIT = 120_000


def _list_design_mockups(workspace: str) -> str:
    """Designer가 만든 실제 HTML 목업(design/applied/*.html — 머지 전이면
    design/pending/*.html로 폴백) 전체를 모아 LLM 컨텍스트로 만든다. 예전엔
    시나리오 테스트를 짤 때 design 스테이지의 요약 텍스트(summary)만 줬는데,
    텍스트 요약은 사람이 다시 압축한 설명이라 버튼 라벨/문구/레이아웃 같은
    세부사항이 요약 과정에서 빠지기 쉽다 — 실제 목업 HTML을 그대로 주면 그런
    손실 없이 정확한 문구/구조를 그대로 테스트에 반영할 수 있다. _list_source_files와
    같은 이유로 파일 단위로는 자르지 않고 전체 합계에만 안전장치를 둔다."""
    applied_dir = f"{workspace}/design/applied"
    mockup_dir = applied_dir if os.path.isdir(applied_dir) else f"{workspace}/design/pending"
    if not os.path.isdir(mockup_dir):
        return "(디자인 목업 없음 — design/applied, design/pending 둘 다 없음)"
    lines = []
    for fname in sorted(os.listdir(mockup_dir)):
        if not fname.endswith(".html"):
            continue
        path = os.path.join(mockup_dir, fname)
        try:
            with open(path, errors="replace") as f:
                content = f.read()
        except OSError:
            content = ""
        lines.append(f"### design/{'applied' if mockup_dir == applied_dir else 'pending'}/{fname}\n{content}")
    result = "\n\n".join(lines) if lines else "(목업 디렉토리는 있지만 .html 파일이 없음)"
    if len(result) > MOCKUP_TOTAL_EXCERPT_LIMIT:
        result = (
            result[:MOCKUP_TOTAL_EXCERPT_LIMIT]
            + "\n... (이하 생략 — 전체 목업이 비정상적으로 커서 잘림)"
        )
    return result


SCENARIO_MANIFEST_FILE = "qa_scenarios.json"


def load_scenario_manifest(workspace: str) -> dict:
    """git에 커밋해둔 시나리오 매니페스트를 읽는다 — 이미 통과한 시나리오를
    다음 라운드에서 또 LLM에 통째로 다시 판단시키지 않기 위한 참조 자료
    (토큰 절약). 없으면 빈 목록으로 시작."""
    path = f"{workspace}/{SCENARIO_MANIFEST_FILE}"
    if not os.path.exists(path):
        return {"scenarios": []}
    try:
        with open(path, errors="replace") as f:
            return json.load(f)
    except Exception:
        return {"scenarios": []}


def save_scenario_manifest(workspace: str, manifest: dict):
    with open(f"{workspace}/{SCENARIO_MANIFEST_FILE}", "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def upsert_scenarios(manifest: dict, titles: list[str], status: str, note: str = ""):
    by_title = {s["title"]: s for s in manifest["scenarios"]}
    for title in titles:
        by_title[title] = {"title": title, "status": status, "note": note}
    manifest["scenarios"] = list(by_title.values())


def git_commit_and_push(workspace: str, message: str, branch: str | None,
                         paths: list[str] = (SCENARIO_MANIFEST_FILE,)) -> bool:
    """QA도 자기 산출물(시나리오 매니페스트 + 생성한 시나리오 테스트 파일)을
    스스로 커밋+푸시한다. 테스트 파일도 커밋해둬야 다음 QA 라운드는 물론
    GitHub Actions CI(flutter test)에서도 계속 실행돼서 회귀를 막아준다.
    주의: QA는 항상 구현 PR의 feature 브랜치를 체크아웃한 상태이므로, 여기서
    main으로 바로 푸시하면 PR/리뷰를 건너뛰고 코드가 병합돼버린다 — 반드시
    지금 체크아웃된 그 브랜치로 푸시해서 같은 PR 안에 들어가게 한다."""
    if not GITHUB_TOKEN or not branch or not os.path.exists(f"{workspace}/.git"):
        return False
    try:
        def run_git(cmd):
            return subprocess.run(cmd, cwd=workspace, capture_output=True, text=True, timeout=60)
        run_git(["git", "config", "user.email", "ai-team-manager@bot"])
        run_git(["git", "config", "user.name", "AI Team Manager"])
        run_git(["git", "add", "-A", *paths])
        commit = run_git(["git", "commit", "-m", message])
        if commit.returncode != 0:
            return False
        push = run_git(["git", "push", "origin", f"HEAD:{branch}"])
        if push.returncode != 0:
            print(f"[qa] git push 실패: {push.stderr[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[qa] git 작업 실패: {e}")
        return False


def _pubspec_package_name(workspace: str) -> str | None:
    path = f"{workspace}/pubspec.yaml"
    if not os.path.exists(path):
        return None
    with open(path, errors="replace") as f:
        for line in f:
            m = re.match(r"^name:\s*(\S+)", line)
            if m:
                return m.group(1)
    return None


_TESTWIDGETS_TITLE_RE = re.compile(r"testWidgets\(\s*['\"](.+?)['\"]")
_DART_CODE_BLOCK_RE = re.compile(r"```dart\s*(.*?)```", re.DOTALL)
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_object(text: str) -> dict:
    """LLM이 JSON을 ```json ... ``` 코드펜스로 감싸서 답하는 경우가 흔한데,
    그럴 때 first-'{'~last-'}' 방식으로 그냥 자르면 펜스 뒤에 붙은 다른 텍스트나
    또 다른 중괄호까지 같이 잘려서 "Extra data" 파싱 에러가 나던 실제 버그가
    있었다(design_qa_check에서 재현됨). 코드펜스를 우선 찾고, 없으면 기존
    방식으로 폴백한다."""
    m = _JSON_BLOCK_RE.search(text)
    if m:
        return json.loads(m.group(1))
    return json.loads(text[text.find("{"):text.rfind("}") + 1])

SCENARIO_TEST_FILE = "integration_test/scenario_test.dart"


def ensure_integration_test_dependency(workspace: str) -> bool:
    """SCENARIO_TEST_FILE이 `integration_test` 패키지(IntegrationTestWidgetsFlutterBinding)를
    쓰는데, `flutter create`가 만드는 기본 pubspec.yaml엔 이게 안 들어 있다 — 없으면
    `flutter test`가 곧바로 "Target file ... integration_test 패키지 없음" 에러로
    죽는다. LLM(OpenHands/생성 프롬프트)에게 pubspec을 알아서 고치라고 맡기지 않고
    여기서 결정적으로 보장한다 — 한 번 빠뜨리면 이후 모든 QA 라운드가 계속
    실패하는 사고로 이어지기 쉬운 종류의 설정이라서다. 이미 있으면 아무것도 안
    건드린다(idempotent). 반환값은 실제로 추가했는지 여부(로그용)."""
    path = f"{workspace}/pubspec.yaml"
    if not os.path.exists(path):
        return False
    with open(path, errors="replace") as f:
        lines = f.read().splitlines()
    if any(re.match(r"^\s*integration_test\s*:", ln) for ln in lines):
        return False

    insertion = ["  integration_test:", "    sdk: flutter"]
    dev_deps_idx = next((i for i, ln in enumerate(lines) if re.match(r"^dev_dependencies\s*:\s*$", ln)), None)
    if dev_deps_idx is not None:
        lines[dev_deps_idx + 1:dev_deps_idx + 1] = insertion
    else:
        lines += ["", "dev_dependencies:", *insertion]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return True


def _extract_scenario_test_code(text: str, stop_reason: str | None) -> tuple[str | None, str | None]:
    """LLM 응답에서 완전한 ```dart 코드 블록만 안전하게 뽑아낸다.
    (code, None)을 반환하거나, 실패하면 (None, 사유)를 반환한다.

    시나리오가 라운드마다 누적돼서(이미 통과한 것도 프롬프트에 계속 쌓임) 응답이
    max_tokens에서 잘리는 경우가 실제로 있었다 — 그러면 닫는 ```가 없어서
    _DART_CODE_BLOCK_RE가 매치를 못 하는데, 예전엔 그때 원문 텍스트를 그대로
    .dart 파일에 썼다. 그 결과 "```dart" 마커까지 소스에 그대로 남아 컴파일
    자체가 깨졌는데, 이게 "시나리오 실패"로 보고돼서 Implement가 이미 맞게
    구현했는데도 재작업 예산(MAX_QA_RETRIES)만 낭비하는 사고가 있었다
    (counter-app에서 실제로 재현 — 3/3 예산을 이 버그 하나가 다 씀). 완전한
    코드 블록을 못 찾으면 절대 원문을 그대로 쓰지 말고 건너뛴다(verdict=skip)."""
    if stop_reason == "max_tokens":
        return None, "생성된 테스트가 길이 제한에 걸려 잘렸습니다 — 이번 라운드는 건너뜁니다"
    m = _DART_CODE_BLOCK_RE.search(text)
    if not m:
        return None, "생성된 응답에서 완전한 ```dart 코드 블록을 찾지 못해 이번 라운드는 건너뜁니다"
    code = m.group(1).strip()
    if not code:
        return None, "LLM이 테스트 코드를 생성하지 않음"
    return code, None


_VERIFY_SCENARIOS_RULES = """당신은 Flutter QA 엔지니어입니다. 이 system 메시지의 다음 블록엔 이
프로젝트의 PM 요구사항, Designer 스펙, Designer가 실제로 만든 화면 목업(HTML)이 있고, 사용자
메시지엔 이번 라운드 지시사항 · 이미 통과 확인된 시나리오 목록 · 실제 소스 코드가 있습니다.
"이번 라운드 지시사항"에 명시된 범위에 대해, 아직 검증 안 된 핵심 시나리오(화면/기능, 사용자
관점에서 이름 붙일 것) 각각을 실제로 실행해서 확인하는 Flutter 위젯 테스트 파일을 작성하세요.
전체 PRD를 다 테스트할 필요는 없습니다 — 점진적으로 기능을 늘려가는 워크플로에서 "이번엔
화면 2개만" 요청했는데 PRD 전체 기준으로 테스트를 쓰면 매번 실패해서 재작업 루프가 끝없이
돕니다.

규칙:
- `package:flutter/material.dart`, `package:flutter_test/flutter_test.dart`,
  `package:integration_test/integration_test.dart`, 그리고 이 프로젝트의 main.dart(정확한
  패키지 이름은 "이 프로젝트의 Flutter 패키지 이름" 블록 참고)를 반드시 import하세요 —
  material.dart가 없으면 Scaffold/Column/ElevatedButton 같은 기본 위젯 이름을 못 찾아
  컴파일이 실패합니다. integration_test는 이 테스트를 로컬 시뮬레이션뿐 아니라 나중에
  실제 기기(Firebase Test Lab)에서도 그대로 돌리기 위해 반드시 필요합니다.
- `main()` 함수의 첫 줄은 반드시 `IntegrationTestWidgetsFlutterBinding.ensureInitialized();`
  여야 합니다 — 이게 없으면 실제 기기에서 실행할 때 테스트가 즉시 실패합니다.
- 시나리오 하나당 `testWidgets('사용자 관점 시나리오 이름', (tester) async { ... })` 하나.
- 실제로 `tester.pumpWidget(...)`, `tester.tap(...)`, `tester.pump()`, `expect(...)`로 동작을
  조작하고 검증하세요 — 클래스가 존재하는지만 확인하는 게 아니라 실제 동작을 확인해야 합니다.
- 클래스/위젯/아이콘 이름과 화면에 보이는 문구/버튼 라벨은 실제 소스 코드와 Designer 목업
  HTML에 실제로 있는 것만 쓰세요. 지어내지 마세요 — 목업 HTML이 텍스트 스펙보다 정확한
  출처이니 문구/라벨은 목업 기준으로 맞추세요.
- `Center`, `Column`, `Row`, `Padding`, `SizedBox`처럼 MaterialApp/Scaffold 내부에서도
  흔히 쓰이는 범용 레이아웃 위젯은 화면에 여러 개 있는 게 정상입니다. `find.byType(Center)`
  같은 걸 `findsOneWidget`으로 검증하지 마세요 — 실제로 프레임워크 내부 위젯까지 겹쳐서
  개수가 안 맞아 실패하는 게 반복적으로 확인됐습니다. 레이아웃/정렬 확인은 특정 텍스트/버튼이
  존재하는지(`find.text`, `find.byIcon`, `find.byType(FloatingActionButton)` 등 화면에
  하나뿐인 구체적 위젯)로 검증하세요.
- 히스토리/로그/리스트처럼 같은 텍스트가 여러 번 반복될 수 있는 화면(예: 증가를 두 번 눌러서
  "+1"이 두 번 나타나는 경우)에서는 `find.text('+1')`에 `findsOneWidget`을 쓰지 마세요 —
  정확히 몇 개인지 셀 수 있으면 `findsNWidgets(n)`을, 몇 개인지 시나리오상 확실치 않으면
  `findsWidgets`를 쓰세요. `findsOneWidget`은 그 텍스트/위젯이 화면에 정확히 하나만 있어야
  의미가 있는 경우에만 쓰세요.
- 전체 목표에는 있지만 이번 라운드 범위 밖인 기능은 테스트하지 마세요.
- 다른 설명 없이, 파일 전체를 ```dart 코드 블록 하나 안에만 출력하세요."""


async def verify_scenarios(project_id: str, workspace: str, context: dict, instruction: str, already_passed: list[str]) -> dict:
    """이번 라운드에 실제로 요청된 범위(instruction)를 코드가 구현하고 있는지
    "실제로 실행해서" 검증한다. 예전엔 LLM이 소스 코드 텍스트를 읽고 "구현된
    것 같다"고 판단만 했는데(정적 리뷰) — 화면에 안 보이는 위젯도 "없다"고
    오판하는 등 실행 없이 판단하는 데서 오는 오판이 실제로 있었다. 이제는 LLM이
    PM/Designer 산출물 + 실제 소스코드를 보고 이번 라운드 시나리오를 검증하는
    Flutter 위젯 테스트 코드를 직접 작성하게 하고, 그걸 `flutter test`로 실제
    실행해서 통과 여부를 판정한다 — 판단이 아니라 실행 결과.
    전체 PRD(pm_summary)는 참고용일 뿐 — 점진적으로 기능을 늘려가는 워크플로에서
    "이번엔 화면 2개만" 요청했는데 PRD 전체(결제/알림 등) 기준으로 테스트를 쓰면
    매번 실패해서 재작업 루프가 끝없이 돈다.
    already_passed(매니페스트에 이미 pass로 기록된 시나리오 제목)는 프롬프트에
    "재확인 불필요"로만 짧게 넘겨서 토큰을 아낀다.

    PM/Designer 산출물은 예전에 각각 2000자/1500자로 잘라서 넘겼는데, 요약을
    한 번 더 자르면 뒤쪽 시나리오/세부사항이 통째로 안 보여서 LLM이 실제로
    구현된 기능도 "명시 안 됐으니 범위 밖"으로 오판할 수 있었다(build_summary가
    똑같은 이유로 잘림을 없앤 것과 동일한 문제) — 이제 전체를 그대로 넘긴다.
    Designer 텍스트 스펙 대신/추가로 실제 HTML 목업(_list_design_mockups)도
    넘긴다 — 텍스트 요약은 사람이 다시 압축한 설명이라 정확한 문구/버튼 라벨이
    빠지기 쉽지만, 목업 HTML은 그 자체가 원본이라 더 정확하다.

    PM/Designer 산출물·목업·규칙(_VERIFY_SCENARIOS_RULES)은 system 메시지에 넣고
    캐싱한다 — 규칙은 모든 프로젝트의 모든 QA 라운드에서 완전히 동일한 텍스트라
    조직 전체에서 캐시가 공유되고, PM/Design/목업은 이 프로젝트 안에서 design이
    재작업되지 않는 한 라운드가 바뀌어도 그대로라 프로젝트 내 재사용이 된다.
    매 라운드 실제로 바뀌는 건 소스 코드(구현이 진행되며 계속 바뀜)와
    instruction/already_passed뿐이라 그건 user 메시지로 뒤에 붙인다."""
    if not ANTHROPIC_API_KEY:
        return {"verdict": "skip", "detail": "ANTHROPIC_API_KEY 없어 시나리오 검증 스킵"}

    package_name = _pubspec_package_name(workspace)
    if not package_name:
        return {"verdict": "skip", "detail": "pubspec.yaml에서 package name을 못 찾음"}

    pm_summary = ""
    if isinstance(context.get("planning"), dict):
        pm_summary = str(context["planning"].get("summary", ""))
    design_summary = ""
    if isinstance(context.get("design"), dict):
        design_summary = str(context["design"].get("summary", ""))
    mockup_excerpt = _list_design_mockups(workspace)

    # _list_source_files가 자체적으로 SOURCE_TOTAL_EXCERPT_LIMIT까지만 담고
    # 그 이상은 명시적으로 표시하므로 여기서 또 잘라낼 필요가 없다 — 예전에
    # 여기서 다시 자르다가(파일당 한도를 올려도 호출부에서 재차 6000자로
    # 잘라버려서) 같은 문제가 재발한 적이 있었다.
    source_excerpt = _list_source_files(workspace)
    already_str = ", ".join(already_passed) if already_passed else "(없음)"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system_blocks = [
        {
            "type": "text",
            "text": _VERIFY_SCENARIOS_RULES,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
        {
            "type": "text",
            "text": f"""## 이 프로젝트의 Flutter 패키지 이름
{package_name}

## 이 프로젝트의 PM 요구사항 (전체)
{pm_summary or "(없음)"}

## 이 프로젝트의 Designer 화면/컴포넌트 스펙 (전체)
{design_summary or "(없음)"}

## Designer가 실제로 만든 화면 목업 (HTML — 정확한 문구/버튼 라벨/레이아웃은 텍스트
스펙이 아니라 이 목업 기준으로 판단하세요)
{mockup_excerpt}""",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
    ]
    user_prompt = f"""## 이번 라운드 지시사항 (검증 기준 — 이 범위만 테스트하세요)
{instruction or "(없음)"}

## 이미 이전 라운드에 통과 확인된 시나리오 (테스트 다시 안 만들어도 됨)
{already_str}

## 실제 소스 코드 (lib/ 안 .dart 파일들 — 클래스/위젯 이름은 반드시 여기 있는 그대로 쓰세요)
{source_excerpt}"""

    try:
        resp = client.messages.create(
            model=QA_MODEL,
            max_tokens=4096,
            system=system_blocks,
            messages=[{"role": "user", "content": user_prompt}],
        )
        _track_usage(resp)
        code, skip_reason = _extract_scenario_test_code(resp.content[0].text, resp.stop_reason)
        if skip_reason:
            print(f"[qa] 시나리오 테스트 추출 건너뜀: {skip_reason}")
            return {"verdict": "skip", "detail": skip_reason}
    except Exception as e:
        print(f"[qa] 시나리오 테스트 생성 실패: {e}")
        return {"verdict": "skip", "detail": f"테스트 생성 중 에러: {e}"}

    titles = _TESTWIDGETS_TITLE_RE.findall(code)
    if not titles:
        return {"verdict": "skip", "detail": "생성된 코드에서 testWidgets 시나리오를 못 찾음"}

    test_path = f"{workspace}/{SCENARIO_TEST_FILE}"
    os.makedirs(os.path.dirname(test_path), exist_ok=True)
    with open(test_path, "w") as f:
        f.write(code)

    ensure_integration_test_dependency(workspace)
    result = run(["flutter", "test", SCENARIO_TEST_FILE], cwd=workspace, timeout=180)
    detail = f"{result.stdout[-1500:]}\n{result.stderr[-500:]}".strip()
    if result.returncode == 0:
        return {"verdict": "pass", "covered": titles, "missing": [], "detail": detail}
    return {"verdict": "fail", "covered": [], "missing": titles,
            "detail": f"생성된 테스트({SCENARIO_TEST_FILE})가 실행됐지만 실패했습니다:\n{detail}"}


def _ffmpeg_transcode_cmd(src_path: str, dest_path: str) -> list[str]:
    """H.264/AAC 재인코딩 ffmpeg 커맨드 — 순수 함수로 분리해서 실제 ffmpeg 실행
    없이 인자 구성만 테스트할 수 있게 한다."""
    return [
        "ffmpeg", "-y", "-i", src_path,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-movflags", "+faststart",
        dest_path,
    ]


def _transcode_to_h264(src_path: str, dest_path: str) -> bool:
    """Firebase Test Lab이 주는 video.mp4는 실제로는 VP9 스트림이 MP4 컨테이너에
    들어있다(ffprobe로 실측: codec_tag_string=vp09) — 데스크톱 Chrome은 그냥
    재생해버리지만 모바일 Chrome의 <video> progressive playback은 이 조합을
    거부해서 "맥에서는 재생되는데 모바일에서는 안 됨"으로 나타난다(counter-app에서
    실제로 재현됨). 어디서든 똑같이 재생되는 H.264/AAC로 미리 다시 인코딩해서
    저장한다. +faststart로 moov atom을 앞으로 옮겨서 Range 기반 프로그레시브
    재생과도 잘 맞는다."""
    result = run(_ffmpeg_transcode_cmd(src_path, dest_path), timeout=180)
    if result.returncode != 0:
        print(f"[qa] ffmpeg 재인코딩 실패: {result.stderr[-500:]}")
        return False
    return os.path.exists(dest_path) and os.path.getsize(dest_path) > 0


def download_video(gcs_path: str, dest_path: str) -> bool:
    if not gcs_path:
        return False
    # 결과 버킷 트리 안에서 video.mp4를 찾아 첫 번째 것만 받는다
    ls = run(["gcloud", "storage", "ls", "-r", gcs_path.rstrip("/") + "/**"], timeout=60)
    video_paths = [l for l in ls.stdout.splitlines() if l.strip().endswith("video.mp4")]
    if not video_paths:
        print(f"[qa] video.mp4를 결과 버킷에서 찾지 못함: {gcs_path}")
        return False

    raw_path = dest_path + ".raw.mp4"
    cp = run(["gcloud", "storage", "cp", video_paths[0].strip(), raw_path], timeout=120)
    if cp.returncode != 0:
        return False

    if _transcode_to_h264(raw_path, dest_path):
        if os.path.exists(raw_path):
            os.remove(raw_path)
        return True

    # ffmpeg가 없거나 변환이 실패해도 QA 자체를 막을 필요는 없다 — 재생 호환성이
    # 떨어지는 원본(VP9)이라도 그대로 서빙하는 게 영상이 아예 없는 것보다 낫다.
    print(f"[qa] 영상 재인코딩 실패 — 원본(VP9) 그대로 사용: {raw_path}")
    os.replace(raw_path, dest_path)
    return True


def _filter_screenshot_paths(ls_output: str, limit: int = 4) -> list[str]:
    """Robo 테스트는 탐색하면서 발견한 화면마다 artifacts/N.png로 실제 스크린샷을
    남긴다(비디오 프레임 추출용 ffmpeg 없이도 바로 쓸 수 있음). output/ 밑의
    sitemap 같은 요약 이미지는 앱 화면이 아니라서 제외한다."""
    return [
        line.strip() for line in ls_output.splitlines()
        if re.search(r"/artifacts/\d+\.png$", line.strip())
    ][:limit]


def download_screenshots(gcs_path: str, dest_dir: str, limit: int = 4) -> list[str]:
    if not gcs_path:
        return []
    ls = run(["gcloud", "storage", "ls", "-r", gcs_path.rstrip("/") + "/**"], timeout=60)
    shots = _filter_screenshot_paths(ls.stdout, limit)
    os.makedirs(dest_dir, exist_ok=True)
    downloaded = []
    for i, gcs_file in enumerate(shots):
        dest = f"{dest_dir}/screenshot_{i}.png"
        cp = run(["gcloud", "storage", "cp", gcs_file, dest], timeout=60)
        if cp.returncode == 0:
            downloaded.append(dest)
    return downloaded


async def design_qa_check(project_id: str, screenshots: list[str], design_summary: str) -> dict:
    """PM/기능 시나리오와는 별개로, 실제 렌더링된 화면이 Designer 스펙(레이아웃/
    색상/컴포넌트)과 맞는지 비전 모델로 비교한다. 기능은 맞아도 디자인이 완전히
    다르게 구현되는 사고를 실제로 발견했다(counter-app: Designer 스펙엔 배경색
    지정이 없었는데 구현은 임의로 핑크/라벤더 배경을 넣음) — 정적 코드 리뷰와
    Robo 크래시 테스트 둘 다 이런 시각적 불일치는 못 잡는다. 실패해도 파이프라인을
    막지는 않는다 — 디자인 일치 여부는 주관적이라 자동으로 재작업을 트리거하면
    잘못된 판단으로 무한 루프를 돌 위험이 크다(구현/QA 재시도 루프에서 이미 겪음),
    사람이 보고 판단하도록 보고만 한다."""
    if not ANTHROPIC_API_KEY or not screenshots:
        return {"verdict": "skip", "detail": "스크린샷 없음 또는 API 키 없음"}

    content = [{"type": "text", "text": f"""아래는 Designer가 작성한 화면 스펙과, 실제 기기에서
Robo 테스트 중 캡처된 스크린샷입니다. 스펙에 명시된 레이아웃/색상/컴포넌트 배치와
실제 화면이 일치하는지 비교하세요. 시스템 팝업이나 기기 정보 화면처럼 앱과 무관한
스크린샷은 무시하세요.

## Designer 스펙
{design_summary or "(스펙 없음)"}

반드시 아래 JSON 형식으로만 답하세요:
{{"verdict": "match 또는 mismatch 또는 unclear", "detail": "한두 문장으로 일치/불일치 내용"}}"""}]
    for path in screenshots:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=QA_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": content}],
        )
        _track_usage(resp)
        text = resp.content[0].text
        return _extract_json_object(text)
    except Exception as e:
        print(f"[qa] 디자인 QA 비교 실패: {e}")
        return {"verdict": "skip", "detail": f"비교 중 에러: {e}"}


def _should_reuse_implement_apk(build_cmd: list[str], implement_outputs: dict, apk_exists: bool) -> bool:
    """Implement(agents/implement_openhands/run.py)가 남긴 handoff APK를 재빌드
    없이 그대로 쓸지 판단하는 순수 로직만 분리 — QA 자체 빌드(flutter/gradle,
    Firebase 인증 등 무거운 의존성)까지 실행하지 않고도 이 판단만 단위 테스트할
    수 있게 한다.

    세 조건을 모두 만족해야 재사용한다:
    1. qa flavor 같은 특수 빌드 커맨드가 아니라 일반 디버그 빌드일 것 —
       Implement는 항상 plain `flutter build apk --debug`만 만들기 때문에,
       특수 커맨드가 필요한 프로젝트는 그 커맨드로 QA가 직접 다시 빌드해야 한다.
    2. Implement 쪽 빌드가 실제로 성공했을 것(build_ok is True — 값이 아예 없는
       구버전 세션이나 False인 실패 케이스는 재사용하면 안 됨).
    3. handoff 파일이 실제로 존재할 것."""
    return (
        build_cmd == ["flutter", "build", "apk", "--debug"]
        and implement_outputs.get("build_ok") is True
        and apk_exists
    )


def _resolve_target_branch(context: dict) -> str | None:
    """"implement"이 이번 라운드에 실제로 작업한 브랜치의 유일한 권위있는
    출처다. 예전엔 context에 있는 아무 스테이지에서나 "branch" 키를 찾아서
    마지막으로 발견된 값으로 덮어썼는데, "autotest"도 자기 CI 추적용으로
    "branch" 필드를 갖고 있어서 완료된 지 몇 주 된 낡은 autotest 결과가
    방금 implement가 만든 새 브랜치를 덮어써버렸다 — QA가 몇 주 전의 죽은
    브랜치를 계속 테스트하는 사고로 실제 발견됨(디자인이 전혀 반영 안 된
    것처럼 보였던 근본 원인). implement를 최우선으로 보고, 없을 때만 다른
    스테이지를 폴백으로 본다."""
    implement_outputs = context.get("implement")
    if isinstance(implement_outputs, dict) and implement_outputs.get("branch"):
        return implement_outputs["branch"]
    for name, stage_outputs in context.items():
        if name != "implement" and isinstance(stage_outputs, dict) and "branch" in stage_outputs:
            return stage_outputs["branch"]
    return None


async def process_task(r: aioredis.Redis, task: dict):
    _token_usage["input"] = _token_usage["output"] = _token_usage["cache_read"] = _token_usage["cache_write"] = 0
    project_id  = task.get("project_id", "")
    stage       = task.get("stage")
    instruction = task.get("instruction", "")
    context     = task.get("context", {})
    github_repo = task.get("github_repo", "")
    # implement 에이전트도 같은 shared-workspace 볼륨의 /workspace/{project_id}를
    # 자기 git 루트로 쓴다 — QA가 거기서 같이 체크아웃/빌드하면 두 에이전트가
    # 동시에 같은 프로젝트를 처리할 때 서로의 워킹트리를 덮어써서 "local changes
    # would be overwritten"나 알 수 없는 컴파일 에러가 나던 사고가 실제로
    # 있었다. 처음엔 /workspace/{project_id}/.qa-clone(implement 트리 "안")에
    # 격리했는데, 그러면 implement가 git add를 넓게 할 때 .qa-clone/.git을
    # 진짜 서브모듈로 착각해서 커밋해버리는 사고가 또 있었다("No url found for
    # submodule path '.qa-clone'"로 CI가 깨짐) — implement 트리 밖의 완전히
    # 별도 경로를 써야 진짜로 안 섞인다. 오케스트레이터가 서빙하는 산출물
    # (영상/스크린샷)만 기존 경로(output_dir)에 그대로 둔다.
    output_dir  = f"/workspace/{project_id}"
    workspace   = f"/workspace/{project_id}-qa-clone"

    branch = _resolve_target_branch(context)

    await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                    "content": f"[QA] '{stage}' 시작 — Firebase Test Lab Robo 테스트 준비 중 (branch={branch or 'main'})"})

    if not github_repo:
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": "⚠️ 연결된 GitHub 레포가 없어 QA를 건너뜁니다 (통과 처리)."})
        await emit(r, {"type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME, "stage": stage,
                        "outputs": {"agent": AGENT_NAME, "passed": True, "summary": "대상 없음 — 스킵"}})
        return

    if not ensure_gcloud_auth():
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": "❌ Firebase Test Lab 인증 정보(GOOGLE_APPLICATION_CREDENTIALS)가 없어 QA를 진행할 수 없습니다."})
        await emit(r, {"type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME, "stage": stage,
                        "outputs": {"agent": AGENT_NAME, "passed": False, "summary": "Test Lab 인증 실패"}})
        return

    os.makedirs(workspace, exist_ok=True)
    if not os.path.exists(f"{workspace}/.git"):
        repo_url = f"https://{GITHUB_TOKEN}@github.com/{github_repo}.git"
        cp = run(["git", "clone", repo_url, workspace], timeout=180)
        if cp.returncode != 0:
            await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                            "content": f"❌ 레포 clone 실패: {cp.stderr[:300]}"})
            return

    run(["git", "fetch", "origin"], cwd=workspace, timeout=60)
    # _cap_gradle_memory가 android/gradle.properties를 커밋 없이 워킹트리에 직접
    # 써서, 다음 라운드가 다른 브랜치로 체크아웃하려 할 때 "local changes would
    # be overwritten"로 막히던 사고가 있었다(실제 재현됨). workspace는 매번
    # 새로 빌드하는 일회용 스크래치 디렉토리라 로컬 변경을 보존할 이유가 없다 —
    # 브랜치 전환 전에 무조건 깨끗하게 되돌린다.
    run(["git", "reset", "--hard"], cwd=workspace, timeout=30)
    run(["git", "clean", "-fd"], cwd=workspace, timeout=30)
    checkout_target = f"origin/{branch}" if branch else "origin/main"
    checkout = run(["git", "checkout", "-B", branch or "qa-check", checkout_target], cwd=workspace, timeout=30)
    if checkout.returncode != 0:
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"❌ 브랜치 체크아웃 실패({checkout_target}): {checkout.stderr[:300]}"})
        return

    manifest = load_scenario_manifest(workspace)
    already_passed = [s["title"] for s in manifest["scenarios"] if s.get("status") in ("automated_pass", "manual_needed")]

    await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                    "content": "🔍 PM 요구사항 대비 시나리오 구현 여부 검증 중..."})
    scenario = await verify_scenarios(project_id, workspace, context, instruction, already_passed)
    if scenario.get("verdict") == "fail":
        missing = scenario.get("missing", [])
        detail = scenario.get("detail", "")
        feedback = f"다음 시나리오가 실행 테스트({SCENARIO_TEST_FILE})에서 실패했습니다: {', '.join(missing)}\n{detail}"
        upsert_scenarios(manifest, missing, "fail", detail)
        save_scenario_manifest(workspace, manifest)
        git_commit_and_push(workspace, "qa: 시나리오 테스트 갱신 (실패 발견)", branch,
                             paths=[SCENARIO_MANIFEST_FILE, SCENARIO_TEST_FILE])
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"🔁 시나리오 검증 실패 — Implement에 재작업이 필요합니다.\n{feedback}"})
        await emit(r, {"type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME, "stage": stage,
                        "outputs": {"agent": AGENT_NAME, "passed": False, "needs_rework": True,
                                    "feedback": feedback, "summary": "시나리오 검증 실패"}})
        return
    if scenario.get("verdict") == "pass":
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"✅ 시나리오 검증 통과: {scenario.get('detail', '')}"})

        # 로컬 flutter test(시뮬레이션)만으로 끝내지 않고, 같은 테스트를 실제 기기
        # (Firebase Test Lab instrumentation)에서 한 번 더 돌려 "결과물(빌드된 앱을
        # 실기기에서 실행한 결과) 기반"으로 확인한다. 로컬 시뮬레이션과 실기기
        # 사이엔 타이밍/실제 렌더링/플랫폼 채널 등 시뮬레이션이 못 잡는 차이가
        # 있을 수 있어서다. 인프라성 빌드 실패(Android 프로젝트 구조 문제 등)는
        # 앱 로직 버그가 아닐 수 있으므로 재작업을 강제하지 않고 보고만 한다 —
        # 실제로 테스트가 "실행됐지만 실패"한 경우만 needs_rework로 취급한다.
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": "📱 같은 시나리오 테스트를 실제 기기(Firebase Test Lab)에서 재확인합니다..."})
        instr_ok, instr_detail, instr_app_apk, instr_test_apk = build_instrumentation_apks(workspace)
        if not instr_ok:
            await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                            "content": f"⚠️ 실기기 확인용 빌드 실패 — 로컬 시뮬레이션 결과만으로 계속 진행합니다: {instr_detail[:300]}"})
        else:
            instr_cmd = [
                "gcloud", "firebase", "test", "android", "run",
                "--type", "instrumentation",
                "--app", instr_app_apk,
                "--test", instr_test_apk,
                "--device", TEST_DEVICE,
                "--timeout", TEST_TIMEOUT,
                "--project", FIREBASE_TEST_PROJECT,
            ]
            if TEST_LAB_RESULTS_BUCKET:
                instr_cmd += ["--results-bucket", TEST_LAB_RESULTS_BUCKET]
            instr_run = run(instr_cmd, timeout=900)
            instr_result = parse_gcloud_output(instr_run.stdout, instr_run.stderr)
            if instr_result["passed"]:
                await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                                "content": f"✅ 실기기 시나리오 테스트 통과: {instr_result['summary']}"})
            else:
                feedback = (
                    f"로컬 시뮬레이션(flutter test)은 통과했지만 실제 기기에서 같은 시나리오 테스트가 "
                    f"실패했습니다 — 실기기에서만 드러나는 문제(타이밍, 실제 렌더링, 플랫폼 채널 등)일 "
                    f"수 있습니다: {instr_result['summary']}"
                )
                await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                                "content": f"🔁 {feedback}"})
                await emit(r, {"type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME, "stage": stage,
                                "outputs": {"agent": AGENT_NAME, "passed": False, "needs_rework": True,
                                            "feedback": feedback, "summary": "실기기 시나리오 테스트 실패"}})
                return

    build_cmd, rework_reason = determine_build_command(workspace)
    if rework_reason:
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"🔁 QA가 테스트할 결과물이 없어 Implement에 재작업이 필요합니다: {rework_reason}"})
        await emit(r, {"type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME, "stage": stage,
                        "outputs": {"agent": AGENT_NAME, "passed": False, "needs_rework": True,
                                    "feedback": rework_reason, "summary": rework_reason}})
        return

    # Implement(agents/implement_openhands/run.py)가 이미 같은 커맨드로 빌드해서
    # /workspace/{project_id}-artifacts/에 남겨둔 APK가 있으면 재빌드하지 않고
    # 그대로 쓴다 — 빌드를 두 번 하지 않을뿐더러, "Implement가 실제로 만든 그
    # 바이너리"를 QA가 검증한다는 원칙에도 맞는다(QA가 새로 빌드한 별개의
    # 바이너리를 테스트하는 게 아니라). qa flavor처럼 특수 빌드 커맨드가 필요한
    # 프로젝트는 Implement의 일반 디버그 빌드로는 안 맞으므로 대상에서 제외한다.
    # Implement 쪽 빌드가 실패했거나(build_ok=False) 파일이 없으면(재작업 재시도,
    # 이 기능 도입 전 세션 등) 안전하게 QA 자체 빌드로 폴백한다.
    implement_outputs = context.get("implement")
    implement_outputs = implement_outputs if isinstance(implement_outputs, dict) else {}
    handoff_apk = f"/workspace/{project_id}-artifacts/{IMPLEMENT_ARTIFACT_APK_NAME}"

    if _should_reuse_implement_apk(build_cmd, implement_outputs, os.path.exists(handoff_apk)):
        apk_path = handoff_apk
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"📦 Implement가 빌드한 APK를 그대로 사용합니다 (재빌드 생략): {os.path.basename(apk_path)}"})
    else:
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"🔨 APK 빌드 중... ({' '.join(build_cmd)})"})

        _cap_gradle_memory(workspace)
        run(["flutter", "pub", "get"], cwd=workspace, timeout=180)
        build = await run_streaming(build_cmd, workspace, 600, r, project_id, "APK 빌드")
        if build.returncode != 0:
            # 예전엔 stdout/stderr를 각각 800자로 자른 뒤 그 합친 문자열을 다시
            # 뒤에서 1000자로 재슬라이스했다 — stderr만으로 1000자를 넘기면
            # stdout 쪽 정보가 통째로 사라지는 복합 버그가 있었다. 필터는
            # 자르기 전 원본 전체를 받아야 실제 에러 줄을 제대로 찾는다.
            error_excerpt = _extract_issue_lines(f"{build.stdout}\n{build.stderr}") or \
                f"{build.stdout[-800:]}\n{build.stderr[-800:]}"
            await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                            "content": f"❌ APK 빌드 실패:\n{error_excerpt}"})
            await emit(r, {"type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME, "stage": stage,
                            "outputs": {"agent": AGENT_NAME, "passed": False, "needs_rework": True,
                                        "feedback": f"APK 빌드 실패. 이 에러를 고쳐서 다시 구현하세요:\n{error_excerpt}",
                                        "summary": "APK 빌드 실패"}})
            return

        apk_path = find_apk(workspace)
        if not apk_path:
            await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                            "content": "❌ 빌드된 APK를 찾지 못했습니다."})
            await emit(r, {"type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME, "stage": stage,
                            "outputs": {"agent": AGENT_NAME, "passed": False, "needs_rework": True,
                                        "feedback": "빌드는 성공했지만 build/app/outputs/flutter-apk/ 안에서 APK 파일을 찾지 못했습니다.",
                                        "summary": "APK 없음"}})
            return

    await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                    "content": f"🐒 Firebase Test Lab에 Robo(무작위) 테스트 제출: {os.path.basename(apk_path)}"})

    cmd = [
        "gcloud", "firebase", "test", "android", "run",
        "--type", "robo",
        "--app", apk_path,
        "--device", TEST_DEVICE,
        "--timeout", TEST_TIMEOUT,
        "--project", FIREBASE_TEST_PROJECT,
    ]
    if TEST_LAB_RESULTS_BUCKET:
        cmd += ["--results-bucket", TEST_LAB_RESULTS_BUCKET]
    test_run = run(cmd, timeout=900)

    result = parse_gcloud_output(test_run.stdout, test_run.stderr)
    await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                    "content": f"{'✅' if result['passed'] else '❌'} Test Lab 결과: {result['summary']}"})

    video_path = f"{output_dir}/qa_recording.mp4"
    video_ok = False
    design_mismatch_feedback = None
    if result["gcs_path"]:
        video_ok = download_video(result["gcs_path"], video_path)

        design_summary = ""
        if isinstance(context.get("design"), dict):
            design_summary = str(context["design"].get("summary", ""))
        if design_summary:
            screenshots = download_screenshots(result["gcs_path"], f"{output_dir}/.qa_screenshots")
            design = await design_qa_check(project_id, screenshots, design_summary)
            verdict = design.get("verdict")
            if verdict == "mismatch":
                design_mismatch_feedback = (
                    f"디자인 QA — 실제 화면이 Designer 스펙과 다릅니다: {design.get('detail', '')}\n"
                    "다른 기능(카운터 로직 등)은 이미 정상이니, 스펙에 명시된 레이아웃/색상/컴포넌트만 맞춰주세요."
                )
                await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                                "content": f"🎨⚠️ 디자인 QA — 스펙과 실제 화면이 다릅니다. Implement에 재작업을 요청합니다:\n{design.get('detail', '')}"})
            elif verdict == "match":
                await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                                "content": f"🎨✅ 디자인 QA 통과: {design.get('detail', '')}"})
            else:
                # unclear/skip도 조용히 넘어가지 않고 남긴다 — 안 그러면 디자인
                # QA가 아예 안 돌았는지, 판단을 못 한 건지 구분이 안 된다.
                await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                                "content": f"🎨❓ 디자인 QA 판단 보류({verdict or 'skip'}): {design.get('detail', '')}"})

    # 이번 라운드에 새로 통과 확인된 시나리오를 매니페스트에 반영 — Robo 테스트까지
    # 실제로 돌아서 성공했으면 "자동 검증 완료", 코드 근거만 있고 실제 실행 확인은
    # 안 됐으면(Robo 실패/스킵) "수동 확인 필요"로 내려서 사람이 볼 걸 명확히 표시.
    new_scenarios = scenario.get("covered", [])
    if new_scenarios:
        status = "automated_pass" if (result["passed"] and video_ok) else "manual_needed"
        note = f"{SCENARIO_TEST_FILE} 실행 테스트 통과 + Firebase Test Lab Robo 테스트로 자동 확인됨" \
            if status == "automated_pass" \
            else f"{SCENARIO_TEST_FILE} 실행 테스트는 통과했지만 실제 기기 실행 확인이 안 돼 수동 확인 필요"
        upsert_scenarios(manifest, new_scenarios, status, note)
        save_scenario_manifest(workspace, manifest)
        git_commit_and_push(workspace, "qa: 시나리오 테스트/매니페스트 갱신", branch,
                             paths=[SCENARIO_MANIFEST_FILE, SCENARIO_TEST_FILE])

    manual_count = sum(1 for s in manifest["scenarios"] if s.get("status") == "manual_needed")
    auto_count   = sum(1 for s in manifest["scenarios"] if s.get("status") == "automated_pass")
    summary_lines = [
        f"{'✅' if result['passed'] else '❌'} Test Lab 결과: {result['summary']}",
        f"🧪 시나리오 현황 — 자동 검증 {auto_count}건, 수동 확인 필요 {manual_count}건 (qa_scenarios.json 참고)",
    ]
    if video_ok:
        summary_lines.append(f"🎥 동작 영상: /recordings/{project_id}")
    await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                    "content": "\n".join(summary_lines)})

    outputs = _finalize_qa_outputs(result, video_ok, manual_count, design_mismatch_feedback, _token_usage)
    await emit(r, {
        "type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME, "stage": stage,
        "outputs": outputs,
    })


GROUP_NAME = "workers"


async def ensure_group(r: aioredis.Redis):
    """컨슈머 그룹 생성 (최초 1회) — 재시작마다 스트림 전체를 재생하는 걸
    막아서 이미 끝난 Test Lab 작업이 반복 실행되지 않게 한다."""
    try:
        await r.xgroup_create(name=STREAM_INBOX, groupname=GROUP_NAME, id="$", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise


async def main():
    if not GITHUB_TOKEN:
        print("[qa] ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    print(f"[qa] Firebase Test Lab 기반 QA 에이전트 시작 (project={FIREBASE_TEST_PROJECT})")
    r = await aioredis.from_url(REDIS_URL, decode_responses=True)
    await ensure_group(r)

    while True:
        try:
            results = await r.xreadgroup(
                GROUP_NAME, "qa", {STREAM_INBOX: ">"}, block=500, count=1
            )
            for _, messages in results:
                for msg_id, fields in messages:
                    await process_task(r, json.loads(fields["payload"]))
                    await r.xack(STREAM_INBOX, GROUP_NAME, msg_id)
        except Exception as e:
            print(f"[qa] Error: {e}", file=sys.stderr)
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
