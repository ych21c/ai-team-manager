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
# Anthropic Models API(GET /v1/models/claude-haiku-4-5-20251001)로 직접 확인한
# 이 모델의 실제 출력 한도 — max_tokens를 이보다 낮게(예전엔 4096) 잡아두면
# 시나리오가 누적될수록 생성 응답이 중간에 잘리는 사고로 이어진다(이 세션에서
# 실제 재현 — "생성된 테스트가 길이 제한에 걸려 잘렸습니다"로 계속 스킵됨).
# max_tokens 자체는 실제 쓴 토큰만큼만 과금되므로(상한일 뿐, 예약이 아님) 낮춰서
# 아낄 이유가 없다 — 아래 verify_scenarios가 스트리밍으로 이 값을 그대로 쓴다.
QA_MODEL_MAX_TOKENS = 64000
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


# 웹 UI가 "지금 QA가 정확히 뭘 하고 있는지"를 스크롤되는 채팅 로그가 아니라
# 고정된 상태 마커로 보여줄 수 있도록, 의미 있는 작업 단위마다 이 이벤트를
# 쏜다. 예전엔 진행 상황이 print()로 컨테이너 로그에만 남거나(docker logs로만
# 확인 가능) message 이벤트로 채팅에 흘러가 스크롤에 묻혔는데, 특히 자체 수정
# 재시도(scenario_fix)는 print()만 있고 emit이 아예 없어서 웹에서는 QA가 멈춘
# 것처럼 보이는 사고가 실제로 있었다(recoveryfit).
QA_PHASES = {
    "workspace_setup":   "워크스페이스 준비 (clone/checkout)",
    "scenario_generate": "시나리오 테스트 코드 생성",
    "scenario_fix":      "테스트 코드 컴파일 자체 수정",
    "scenario_verify":   "로컬 시나리오 테스트 실행",
    "device_verify":     "실기기 계측 테스트 재확인",
    "build_apk":         "APK 빌드",
    "robo_test":         "Firebase Test Lab Robo 테스트",
    "result_download":   "결과 영상/스크린샷 다운로드",
    "design_qa":         "디자인 QA 비교",
    "finalize":          "결과 집계",
}
_PHASE_ICON = {"start": "⏳", "success": "✅", "fail": "❌", "skip": "➖"}


async def emit_phase(r: aioredis.Redis, project_id: str, stage: str, phase: str,
                      status: str, detail: str = ""):
    label = QA_PHASES.get(phase, phase)
    icon = _PHASE_ICON.get(status, "•")
    print(f"[qa] {icon} [{phase}] {label}" + (f" — {detail}" if detail else ""))
    await emit(r, {
        "type": "agent_phase", "project_id": project_id, "agent": AGENT_NAME,
        "stage": stage, "phase": phase, "label": label, "status": status, "detail": detail,
    })


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


def upsert_scenarios(manifest: dict, titles: list[str], status: str, note: str = "", group: str = ""):
    by_title = {s["title"]: s for s in manifest["scenarios"]}
    for title in titles:
        prev_group = by_title.get(title, {}).get("group", "")
        by_title[title] = {"title": title, "status": status, "note": note,
                            "group": group or prev_group}
    manifest["scenarios"] = list(by_title.values())


# ── 시나리오 테스트 파일의 그룹(화면/기능 단위) 구조 파싱 ─────────────────────
# QA는 매 라운드 scenario_test.dart를 처음부터 통째로 새로 썼었는데, LLM에게는
# 직전 라운드에 자기가 실제로 뭐라고 짰었는지가 전혀 전달되지 않아서(제목만
# "이미 통과함" 힌트로 넘어감) 이미 고쳐둔 테스트가 다음 라운드에 다시 같은
# 실수로 재작성되는 게 반복 확인됐다(recoveryfit에서 실제 재현 — 워드마크
# find.text() 오판, pumpAndSettle 오용, RichText findRichText 누락이 각각
# 최소 2번씩 재발). 이제는 화면/기능 단위로 묶은 `group('이름', () {
# testWidgets(...); ... });` 블록 여러 개가 한 파일에 쌓이는 구조로 보고,
# 이번 라운드 지시사항이 가리키는 그룹만 잘라서 LLM에게 "이미 있으면 그
# 블록만 고치고, 없으면 새 블록만 추가"하도록 넘긴다 — 건드리지 않는 다른
# 그룹은 파이썬이 그대로 보존해서 파일에 다시 합친다(LLM이 아예 못 봄).
_VOID_MAIN_RE = re.compile(r"void\s+main\s*\(\s*\)\s*\{")
_GROUP_START_RE = re.compile(r"\bgroup\(\s*(['\"])((?:\\.|(?!\1).)*)\1\s*,\s*\(\)\s*\{")


def _find_matching_brace(src: str, open_idx: int) -> int | None:
    """src[open_idx]가 '{'라고 가정하고 그 짝이 되는 '}'의 인덱스를 찾는다.
    문자열 리터럴('/"/삼중따옴표)과 주석(//, /* */) 안의 중괄호는 세지 않는다 —
    화면 문구에 중괄호가 섞여 있어도 블록 경계가 잘못 잘리지 않게 하기 위함."""
    depth = 0
    i = open_idx
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            nl = src.find("\n", i)
            i = nl if nl != -1 else n
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = (end + 2) if end != -1 else n
            continue
        if src.startswith("'''", i) or src.startswith('"""', i):
            quote = src[i:i + 3]
            end = src.find(quote, i + 3)
            i = (end + 3) if end != -1 else n
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n and src[j] != ch:
                j += 2 if src[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_group_blocks(body: str) -> list[tuple[str, int, int]] | None:
    """body(void main() {...} 안쪽 텍스트)에서 최상위 group('이름', () {...});
    블록들을 찾아 (그룹 이름, 시작 인덱스, 끝 인덱스) 목록으로 반환한다. 매
    블록을 찾을 때마다 그 블록의 끝까지 커서를 건너뛰고 다음을 찾는다 —
    testWidgets 본문 안의 텍스트를 그룹 시작으로 잘못 다시 매치하지 않기
    위함(중첩 group()은 이 프로젝트의 생성 규칙상 없다고 가정). 형식이
    예상과 다르면(그룹을 하나도 못 찾음) None — 호출부가 안전하게 "파일
    전체 재생성" 모드로 폴백하게 한다."""
    blocks = []
    pos = 0
    while True:
        m = _GROUP_START_RE.search(body, pos)
        if not m:
            break
        open_brace = m.end() - 1
        close = _find_matching_brace(body, open_brace)
        if close is None:
            return None
        end = close + 1
        if body[end:end + 2] == ");":
            end += 2
        blocks.append((m.group(2), m.start(), end))
        pos = end
    return blocks or None


def parse_scenario_groups(source: str) -> dict | None:
    """기존 scenario_test.dart 전체를 header(임포트~`void main() {`)/prelude
    (ensureInitialized() 등 첫 그룹 이전 코드)/groups(그룹 이름, 코드) 목록으로
    쪼갠다. 구조가 예상과 다르면 None. void main()의 진짜 짝 `}`를
    rfind("}")가 아니라 브레이스 매칭으로 찾는다 — 그래야 파일 끝에 다른
    내용이 붙어 있어도 main() 범위를 정확히 잡는다."""
    m = _VOID_MAIN_RE.search(source)
    if not m:
        return None
    open_brace = m.end() - 1
    close = _find_matching_brace(source, open_brace)
    if close is None:
        return None
    header = source[:open_brace + 1]
    body = source[open_brace + 1:close]
    blocks = _split_group_blocks(body)
    if not blocks:
        return None
    prelude = body[:blocks[0][1]].strip()
    groups = [(name, body[s:e].strip()) for name, s, e in blocks]
    return {"header": header.rstrip(), "prelude": prelude, "groups": groups}


def rebuild_scenario_test(parsed: dict) -> str:
    """parse_scenario_groups()가 만든 구조를 다시 하나의 .dart 소스로 합친다.
    Dart는 공백/들여쓰기에 의미를 두지 않으므로 원본 바이트를 그대로 보존할
    필요는 없다 — 건드리지 않은 그룹은 코드 내용 자체(로직)만 원본과 동일하면
    충분하다."""
    parts = [parsed["header"].rstrip(), ""]
    if parsed["prelude"]:
        parts.append("  " + parsed["prelude"])
        parts.append("")
    for _, block in parsed["groups"]:
        parts.append("  " + block)
        parts.append("")
    parts.append("}")
    return "\n".join(parts) + "\n"


def _extract_response_groups(code: str) -> list[tuple[str, str]] | None:
    """LLM 응답이 group(...) 블록 하나 이상만(임포트/void main() 없이) 담고
    있다고 가정하고 파싱한다 — void main() {...} 껍데기로 임시로 감싸서
    parse_scenario_groups를 재사용한다."""
    wrapped = "void main() {\n" + code.strip() + "\n}"
    parsed = parse_scenario_groups(wrapped)
    return parsed["groups"] if parsed else None


def _merge_groups(existing: list[tuple[str, str]],
                   updated: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """이름이 같은 그룹은 교체(수정), 없는 이름은 끝에 추가(신규) — 그 외
    기존 그룹은 순서/내용 그대로 보존한다."""
    merged = list(existing)
    index_by_name = {name: i for i, (name, _) in enumerate(merged)}
    for name, block in updated:
        if name in index_by_name:
            merged[index_by_name[name]] = (name, block)
        else:
            merged.append((name, block))
    return merged


def _group_titles_map(groups: list[tuple[str, str]] | None) -> dict[str, str]:
    """{시나리오 제목: 그룹 이름} — 매니페스트에 어느 그룹 소속인지 같이
    적어두기 위함(qa_scenarios.json을 사람이 훑어볼 때도, 다음 라운드가
    이 그룹을 다시 찾을 때도 유용)."""
    if not groups:
        return {}
    return {t: name for name, block in groups for t in _TESTWIDGETS_TITLE_RE.findall(block)}


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
  `package:integration_test/integration_test.dart`를 반드시 import하세요 — material.dart가
  없으면 Scaffold/Column/ElevatedButton 같은 기본 위젯 이름을 못 찾아 컴파일이 실패합니다.
  integration_test는 이 테스트를 로컬 시뮬레이션뿐 아니라 나중에 실제 기기(Firebase Test
  Lab)에서도 그대로 돌리기 위해 반드시 필요합니다.
- `tester.pumpWidget(...)`에 넘길 최상위 App 위젯(보통 `MaterialApp`을 감싸는
  `FooApp`/`MyApp` 같은 클래스)은 `main.dart`가 아니라 **그 클래스가 실제로 정의된
  파일**을 import하세요 — 많은 프로젝트가 `main.dart`엔 `void main() { runApp(...) }`만
  두고 App 위젯 클래스 자체는 `app.dart` 등 별도 파일에 정의합니다. Dart의 `import`는
  전이적으로 재노출되지 않으므로(`export`가 아닌 한) `main.dart`만 import하면 그 안에서
  `import`한 클래스를 테스트에서 못 찾아 "Couldn't find constructor 'FooApp'"로 컴파일이
  실패합니다(recoveryfit에서 실제 재현: main.dart는 app.dart의 RecoveryFitApp을 import만
  했을 뿐인데 테스트가 main.dart를 import해서 실패). 제공된 소스에서 `runApp(...)`에
  전달되는 클래스명을 확인하고, 그 클래스가 `class` 선언으로 정의된 실제 파일의 패키지
  경로(정확한 패키지 이름은 "이 프로젝트의 Flutter 패키지 이름" 블록 참고)를 import하세요.
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
- 로딩 스피너/점 애니메이션처럼 `AnimationController`가 `.repeat()`으로 영원히 반복되는
  화면(스플래시 화면 등)에서는 `tester.pumpAndSettle()`을 쓰지 마세요 — pumpAndSettle은
  "더 이상 예정된 프레임이 없을 때"까지 기다리는데, 반복 애니메이션은 절대 멈추지 않아서
  항상 타임아웃으로 실패합니다(코드가 맞아도 테스트 자체가 통과 불가능해집니다 — 실제로
  이 패턴 때문에 Implement가 존재하지 않는 버그를 몇 시간째 쫓은 사고가 있었습니다).
  이런 화면에서는 `tester.pump(Duration(...))`로 정확히 원하는 시간만큼만 프레임을
  진행시키세요. 화면이 전환된 뒤(반복 애니메이션이 dispose된 뒤)에는 다시 pumpAndSettle을
  써도 됩니다.
- 애니메이션이 끝난 뒤 실제 비동기 초기화(로컬 DB 오픈, 알림 서비스 초기화 등)까지 끝나야
  다음 화면으로 넘어가는 스플래시류 화면에서는, `tester.pump(Duration(seconds: 3))`처럼
  한 번에 큰 Duration을 pump하지 마세요 — 이런 단발 pump는 프레임/마이크로태스크를 딱 한
  번만 진행시켜서, 애니메이션 이후에 걸린 비동기 체인이 그 한 번 안에 다 안 풀려 화면이
  전환되기 "전" 상태로 남아있는 게 실측으로 확인됐습니다(recoveryfit에서 실제 재현: 3초를
  pump해도 여전히 스플래시 화면). 대신 아래처럼 작게 나눠서 여러 번 pump하며 목표 화면의
  텍스트/위젯이 나타날 때까지 기다리세요:
  ```dart
  Future<void> pumpUntilFound(WidgetTester tester, Finder finder, {int maxTries = 10}) async {
    for (var i = 0; i < maxTries; i++) {
      await tester.pump(const Duration(milliseconds: 500));
      if (finder.evaluate().isNotEmpty) return;
    }
  }
  ```
- 화면 이름을 확인할 때 `find.byType(SplashScreen)`, `find.byType(LandingScreen)`처럼
  이 프로젝트 고유의 화면/위젯 클래스를 참조하지 마세요 — 그 클래스가 정의된 파일을
  import하지 않았으면(이 테스트는 main.dart만 import합니다) 컴파일이 즉시 실패합니다.
  화면 식별은 항상 `find.text(...)`, `find.byIcon(...)`처럼 그 화면에만 있는 문구/아이콘으로
  하세요. `Scaffold`, `MaterialApp`, `ElevatedButton`, `FloatingActionButton`처럼 이미 import된
  flutter/material.dart의 표준 위젯 타입은 `find.byType(...)`에 써도 됩니다.
- 헤드라인/라벨처럼 줄바꿈이 있는 텍스트를 검증할 때, 실제 소스 코드를 보고 그 문구가
  한 `Text` 위젯 안에 `\\n`으로 들어있는지 아니면 여러 줄이 별도의 `Text` 위젯으로 나뉘어
  있는지 반드시 확인하세요. 소스에 `Text('첫째줄')`과 `Text('둘째줄')`처럼 위젯이 나뉘어
  있는데 테스트에서 `find.text('첫째줄\\n둘째줄')`처럼 합친 문자열로 찾으면 항상
  `findsNothing`으로 실패합니다(recoveryfit에서 실제 재현) — 그럴 땐 각 줄을 따로
  `find.text('첫째줄')`, `find.text('둘째줄')`로 검증하세요.
- 반대로 한 `Text`/`Text.rich` 위젯 안에 여러 조각이 합쳐진 문구(예:
  `Text('의료기기 아님 · 전문의 상담을 대체하지 않습니다')` 하나, 또는 문장
  중간에 `TextSpan`으로 일부만 강조된 경우)에서 그 일부만 검증하고 싶으면
  `find.text(...)` 완전 일치 대신 `find.textContaining('일부 문구')`를
  쓰세요 — `find.text()`는 위젯의 전체 텍스트가 정확히 같아야만 매치되므로
  부분 문자열로는 항상 `findsNothing`으로 실패합니다(recoveryfit에서 실제
  재현). 그리고 소스가 `Text.rich(...)`가 아니라 순수 `RichText(text:
  TextSpan(...))` 위젯을 직접 쓰고 있다면 `find.text()`/`find.textContaining()`
  기본 설정으론 아예 못 찾습니다 — `findRichText: true`를 반드시 추가하세요
  (`find.textContaining('일부 문구', findRichText: true)`). 소스에서 그
  텍스트가 `RichText(` 안에 있는지 `Text(`/`Text.rich(` 안에 있는지 먼저
  확인하세요.
- `find.byType(...)`에 쓰는 위젯 타입도 실제 소스 코드 excerpt에 그 타입이
  실제로 등장하는지 먼저 확인하세요 — `CircleAvatar`/`SingleChildScrollView`/
  `Icon`처럼 프로젝트 고유 클래스가 아닌 범용 Flutter 위젯이라도, 소스에 안
  보이면 추측으로 쓰지 마세요(recoveryfit에서 실제 재현: 실제로는
  `Icon(Icons.circle)`인 로딩 점을 `CircleAvatar`로 짐작해서 실패, 스크롤
  없는 단일 뷰포트 레이아웃인데 `SingleChildScrollView`가 있다고 짐작해서
  실패). 레이아웃 비율(예: "히어로 영역이 상단 55%")처럼 위젯 테스트로 픽셀
  단위 검증이 부적절한 시나리오는 특정 위젯 타입 존재 여부로 억지로 검증하지
  말고, 그 화면의 핵심 콘텐츠(텍스트/버튼)가 표시되는지로 충분히 검증하세요.
- 화면 전환 후 `tester.pump(Duration(...))`로 큰 시간을 건너뛰었다면(위
  `pumpUntilFound` 패턴을 안 쓰는 경우), 그 직후 남은 비동기 초기화가 마저
  끝나도록 `await tester.pumpAndSettle(const Duration(milliseconds: 500));`을
  이어서 호출하세요 — 반복 애니메이션이 이미 끝난 화면(목적지 화면)에서는
  안전합니다. 같은 테스트 파일 안에서 이 마무리 호출을 어떤 시나리오엔
  붙이고 어떤 시나리오엔 빠뜨리면, 빠뜨린 쪽만 비동기 체인이 안 끝난 채로
  검증해서 실패합니다(recoveryfit에서 실제 재현) — 화면 전환을 확인하는
  모든 시나리오에 일관되게 붙이세요.
- 이 프로젝트의 시나리오 테스트는 화면/기능 단위로 묶은 `group('이름', () {
  testWidgets(...); ... });` 블록 여러 개가 한 파일(void main() 안)에 쌓이는
  구조입니다. 사용자 메시지에 "이미 있는 시나리오 그룹 목차"가 주어지고,
  이번 라운드 지시사항이 그중 기존 그룹과 같은 화면/기능을 가리키면 그 그룹의
  현재 코드도 함께 주어집니다.
  - 기존 그룹의 코드가 주어졌으면, 그 그룹 이름을 정확히 그대로 써서 그
    group(...) 블록만 다시 작성하세요 — 단, **이번 라운드 지시사항이 실패로
    지목한 시나리오(들)만 고치고, 목차에 없는 새 시나리오만 추가하세요.
    그 외 기존 testWidgets(...)는 주어진 코드에서 단 한 글자도 바꾸지 말고
    그대로 복사해서 넣으세요** — 실패하지 않은 시나리오의 제목을 다듬거나,
    이미 있는 assertion을 "더 낫게" 재작성하거나, 순서를 바꾸는 것도
    금지입니다. 프로젝트 초기라 그룹이 이 하나뿐이면 이 그룹이 사실상 파일
    전체이므로, 이 규칙을 어기면 파일 전체를 처음부터 다시 쓰는 것과
    같아집니다 — 그렇게 다시 쓰다가 이미 고쳐뒀던 실수(예: Text.rich/RichText
    문구를 findRichText 없이 find.text로 찾으려 함)가 그대로 재발하는 사고가
    recoveryfit에서 같은 화면 그룹에 대해 여러 라운드 연속으로 반복됐습니다.
    관련 없는 다른 그룹은 당신에게 아예 주어지지 않습니다 — 신경 쓰지
    마세요, 그대로 보존됩니다.
  - 이번 라운드 지시사항이 목차의 어떤 기존 그룹과도 안 맞으면(새 화면/기능
    검증), 목차에 없는 새 이름으로 새 group(...) 블록 하나를 작성하세요.
  - 응답은 항상 이 group(...) 블록만 출력하세요 — import문이나 void main(),
    다른 그룹은 포함하지 마세요. "이미 있는 시나리오 그룹 목차"가 사용자
    메시지에 없다면(첫 라운드) 이 규칙은 무시하고 예전처럼 파일 전체(임포트
    + void main() + group 블록)를 작성하세요.
- 다른 설명 없이, 위 형식(그룹 블록만 또는 파일 전체) 그대로 ```dart 코드
  블록 하나 안에만 출력하세요."""


def _scenario_test_cmd(test_file: str) -> list[str]:
    """integration_test 패키지 기반 테스트는 flutter-tester가 아니라 실제 Linux
    데스크톱 앱으로 빌드되어 GTK 창을 띄우려 시도한다 — 컨테이너엔 디스플레이가
    없어서 xvfb 없이 그냥 실행하면 빌드는 성공해놓고 "Error waiting for a debug
    connection: The log reader stopped unexpectedly, or never started."로 실행만
    항상 실패한다(recoveryfit에서 실제 재현: 하루 종일 모든 QA 라운드가 이걸로
    실패해서 앱 코드가 맞았는지 틀렸는지조차 한 번도 확인 못 함). xvfb-run으로
    가짜 디스플레이를 띄워서 GTK 창이 붙을 곳을 만들어준다."""
    return ["xvfb-run", "-a", "flutter", "test", test_file]


# QA가 스스로 생성한 scenario_test.dart 자체가 컴파일이 안 되는 경우(예: Flutter
# Finder엔 없는 `.or()`/`.and()` 콤비네이터를 썼다든지)를 나타내는 flutter test
# 출력 신호들. 이건 "앱이 요구사항을 안 지켜서" 실패한 게 아니라 QA가 방금 쓴
# 테스트 코드 자체의 문법 오류라서, Implement에 재작업을 요청해봐야 앱 코드는
# 이미 맞을 수 있어 예산만 날린다(recoveryfit에서 실제 재현 — MAX_QA_RETRIES
# 3회를 이 컴파일 오류 하나가 전부 소진함, 관련 근본 원인은 대화 로그 참고).
_BUILD_FAILURE_MARKERS = (
    "Failed to load", "Build process failed", "Target kernel_snapshot_program failed",
    "Compiler failed", "compilation failed",
)


def _looks_like_build_failure(detail: str) -> bool:
    return any(marker in detail for marker in _BUILD_FAILURE_MARKERS)


MAX_QA_BUILD_FIX_ROUNDS = 2  # 컴파일 실패 한정 자체 수정 시도 횟수(성공 라운드 제외)


async def verify_scenarios(r: aioredis.Redis, project_id: str, stage: str, workspace: str,
                            context: dict, instruction: str,
                            manual_build_fix: bool = False) -> dict:
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

    예전엔 매 라운드 scenario_test.dart 전체를 처음부터 새로 썼다 — LLM에게
    직전 라운드에 자기가 실제로 뭐라고 짰었는지가 전혀 전달되지 않았고("이미
    통과함" 제목 목록만 힌트로 줬음), 그 결과 이미 고쳐둔 실수(워드마크
    find.text() 오판, 스플래시 pumpAndSettle 오용, RichText findRichText
    누락 등)가 다음 라운드에 아무 기억 없이 다시 재발하는 게 recoveryfit에서
    반복 확인됐다. 이제는 화면/기능 단위로 묶은 group('이름', () {
    testWidgets(...); ... }); 블록 여러 개가 한 파일에 누적되는 구조로 보고,
    기존 파일을 parse_scenario_groups()로 그룹 단위로 쪼갠 뒤, 이번 라운드
    지시사항이 가리키는 그룹(있으면)의 현재 코드만 LLM에게 넘겨서 "이미 있으면
    그 블록만 고치고, 없으면 새 블록만 추가"하게 한다 — 관련 없는 다른 그룹은
    LLM에게 아예 보여주지 않고 파이썬이 그대로 보존해서 다시 합친다. 토큰도
    아끼고(전체 스위트가 커져도 매 라운드 비용은 "이번에 건드리는 그룹" 크기로만
    늘어난다), 안 건드린 그룹이 실수로 재작성돼 회귀하는 것도 원천 차단된다.
    기존 파일이 없거나(첫 라운드) 파싱에 실패하면(예상과 다른 형식) 안전하게
    예전 방식(파일 전체 생성)으로 폴백한다.

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
    instruction/그룹 목차/건드릴 그룹 코드뿐이라 그건 user 메시지로 뒤에
    붙인다."""
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

    test_path = f"{workspace}/{SCENARIO_TEST_FILE}"
    existing_source = None
    if os.path.exists(test_path):
        with open(test_path, errors="replace") as f:
            existing_source = f.read()
    parsed = parse_scenario_groups(existing_source) if existing_source else None
    incremental = parsed is not None

    # 이번 라운드 지시사항 문구 안에 기존 그룹의 시나리오 제목이 그대로
    # 언급돼 있으면 그 그룹이 이번 라운드 대상이라고 본다 — 재작업 피드백은
    # 오케스트레이터가 QA 자신이 직전 라운드에 upsert_scenarios()로 저장한
    # 제목을 그대로 인용해서 만들어주므로(_retry_implement_with_feedback),
    # 문자열 포함 여부만으로도 신뢰도 높게 매칭된다.
    touched_names: list[str] = []
    scope_block = ""
    if incremental:
        touched_names = [
            name for name, block in parsed["groups"]
            if any(t and t in instruction for t in _TESTWIDGETS_TITLE_RE.findall(block))
        ]
        index_lines = [
            f"- {name} ({len(_TESTWIDGETS_TITLE_RE.findall(block))}개 시나리오)"
            for name, block in parsed["groups"]
        ]
        scope_block = "## 이 프로젝트에 이미 있는 시나리오 그룹 목차\n" + "\n".join(index_lines)
        if touched_names:
            existing_group_code = "\n\n".join(
                f"### {name}\n```dart\n{block}\n```"
                for name, block in parsed["groups"] if name in touched_names
            )
            scope_block += (
                "\n\n## 이번 라운드에서 이어서 고칠 기존 그룹의 현재 코드\n"
                "(이 그룹 이름 그대로 다시 작성하세요. 이번 라운드 지시사항이 "
                "실패로 지목한 testWidgets(들)만 고치거나, 새 testWidgets를 "
                "추가하세요 — 그 외 아래 testWidgets는 토씨 하나 바꾸지 말고 "
                "그대로 복사해 넣으세요. 목차의 다른 그룹은 이 프롬프트에 아예 "
                "없습니다, 그대로 보존되니 신경 쓰지 마세요)\n"
                f"{existing_group_code}"
            )
        else:
            scope_block += (
                "\n\n(이번 라운드 지시사항은 위 어떤 기존 그룹과도 안 맞습니다 — "
                "목차에 없는 새 이름으로 새 group(...) 블록을 작성하세요)"
            )

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
    scope_section = f"\n{scope_block}\n" if scope_block else ""
    user_prompt = (
        "## 이번 라운드 지시사항 (검증 기준 — 이 범위만 테스트하세요)\n"
        f"{instruction or '(없음)'}\n"
        f"{scope_section}"
        "\n## 실제 소스 코드 (lib/ 안 .dart 파일들 — 클래스/위젯 이름은 반드시 여기 있는 그대로 쓰세요)\n"
        f"{source_excerpt}"
    )

    ensure_integration_test_dependency(workspace)
    messages = [{"role": "user", "content": user_prompt}]

    # incremental 모드에서 "지금까지 확정된 전체 그룹 목록" — 매 시도(attempt)
    # 마다 LLM이 이번에 건드린 그룹만 여기에 병합된다. 안 건드린 그룹은 여기
    # 원본 그대로 남아있다가 rebuild_scenario_test로 파일에 다시 합쳐진다.
    current_groups = list(parsed["groups"]) if incremental else []

    # 라운드 0은 처음 생성, 이후 MAX_QA_BUILD_FIX_ROUNDS번은 "컴파일 실패"에만
    # 한정된 자체 수정 재시도 — 실행은 됐는데 시나리오 자체가 실패한 경우(진짜
    # 앱 버그일 가능성)는 여기서 재시도하지 않고 바로 fail로 보고한다(그건
    # Implement가 고칠 문제라서).
    for attempt in range(MAX_QA_BUILD_FIX_ROUNDS + 1):
        phase = "scenario_generate" if attempt == 0 else "scenario_fix"
        phase_detail = "" if attempt == 0 else f"컴파일 실패 자체 수정 재시도 {attempt}/{MAX_QA_BUILD_FIX_ROUNDS}"
        await emit_phase(r, project_id, stage, phase, "start", phase_detail)
        try:
            # 논스트리밍 client.messages.create()로 max_tokens=64000을 요청하면
            # SDK가 "이 길이면 10분 타임아웃을 넘길 수 있다"고 보고 스트리밍을
            # 요구한다(agents/base/agent.py가 이미 같은 이유로 haiku 역할에
            # 스트리밍을 쓰는 것과 동일한 제약) — 스트리밍으로 끝까지 받은 뒤
            # get_final_message()로 기존 코드가 기대하는 것과 같은 모양(resp.usage/
            # resp.content[0].text/resp.stop_reason)의 객체를 그대로 얻는다.
            with client.messages.stream(
                model=QA_MODEL,
                max_tokens=QA_MODEL_MAX_TOKENS,
                system=system_blocks,
                messages=messages,
            ) as stream:
                resp = stream.get_final_message()
            _track_usage(resp)
            code, skip_reason = _extract_scenario_test_code(resp.content[0].text, resp.stop_reason)
            if skip_reason:
                print(f"[qa] 시나리오 테스트 추출 건너뜀: {skip_reason}")
                await emit_phase(r, project_id, stage, phase, "skip", skip_reason)
                return {"verdict": "skip", "detail": skip_reason}
        except Exception as e:
            print(f"[qa] 시나리오 테스트 생성 실패: {e}")
            await emit_phase(r, project_id, stage, phase, "fail", f"테스트 생성 중 에러: {e}")
            return {"verdict": "skip", "detail": f"테스트 생성 중 에러: {e}"}

        # incremental 모드: 응답이 group(...) 블록(들)만 담고 있다고 기대한다.
        # 그렇게 안 읽히면(LLM이 지시를 무시하고 파일 전체를 냈을 수 있음)
        # 파일 전체로도 시도해보고, 둘 다 실패하면(형식이 완전히 어긋남,
        # 또는 애초에 첫 라운드라 incremental이 아니었던 경우) 받은 텍스트를
        # 그대로 파일로 써서 예전처럼 컴파일만 시도한다.
        response_groups = _extract_response_groups(code) if incremental else None
        if response_groups is not None:
            updated_groups = _merge_groups(current_groups, response_groups)
            full_source = rebuild_scenario_test({
                "header": parsed["header"], "prelude": parsed["prelude"], "groups": updated_groups,
            })
            touched_this_attempt = response_groups
        else:
            whole_file_parsed = parse_scenario_groups(code)
            if whole_file_parsed:
                updated_groups = whole_file_parsed["groups"]
                full_source = rebuild_scenario_test(whole_file_parsed)
                touched_this_attempt = whole_file_parsed["groups"]
            else:
                updated_groups = None
                full_source = code
                touched_this_attempt = None

        titles_this_attempt = (
            [t for _, block in touched_this_attempt for t in _TESTWIDGETS_TITLE_RE.findall(block)]
            if touched_this_attempt is not None
            else _TESTWIDGETS_TITLE_RE.findall(full_source)
        )
        if not titles_this_attempt:
            await emit_phase(r, project_id, stage, phase, "skip", "생성된 코드에서 testWidgets 시나리오를 못 찾음")
            return {"verdict": "skip", "detail": "생성된 코드에서 testWidgets 시나리오를 못 찾음"}
        group_titles = _group_titles_map(touched_this_attempt)

        os.makedirs(os.path.dirname(test_path), exist_ok=True)
        with open(test_path, "w") as f:
            f.write(full_source)
        if updated_groups is not None:
            current_groups = updated_groups

        await emit_phase(r, project_id, stage, phase, "success", "생성 완료 — 컴파일/실행 확인 중")
        await emit_phase(r, project_id, stage, "scenario_verify", "start", SCENARIO_TEST_FILE)
        result = run(_scenario_test_cmd(SCENARIO_TEST_FILE), cwd=workspace, timeout=180)
        detail = f"{result.stdout[-1500:]}\n{result.stderr[-500:]}".strip()

        if result.returncode == 0:
            await emit_phase(r, project_id, stage, "scenario_verify", "success",
                              f"{len(titles_this_attempt)}개 시나리오 통과")
            return {"verdict": "pass", "covered": titles_this_attempt, "missing": [],
                    "detail": detail, "group_titles": group_titles}

        if not _looks_like_build_failure(detail):
            # 실제로 실행은 됐는데 assertion이 실패함. 이번 라운드가 건드린
            # 그룹의 시나리오만 "이번 라운드 실패"로 본다 — 안 건드린 다른
            # 그룹의 기존 실패까지 이번 라운드 탓으로 돌리면, 이미 무관한
            # 문제로 Implement를 계속 재작업시키는 무한 루프가 될 수 있다.
            failed_all = set(re.findall(r"The test description was:\s*(.+)", result.stdout))
            failed_in_scope = [t for t in titles_this_attempt if t in failed_all]
            if not failed_in_scope:
                await emit_phase(r, project_id, stage, "scenario_verify", "success",
                                  "범위 밖 그룹 기존 실패만 있음 — 통과 처리")
                return {"verdict": "pass", "covered": titles_this_attempt, "missing": [],
                        "detail": f"(참고: 이번 라운드 범위 밖 다른 그룹에 기존 실패가 있을 수 있음)\n{detail}",
                        "group_titles": group_titles}
            await emit_phase(r, project_id, stage, "scenario_verify", "fail",
                              f"실패: {', '.join(failed_in_scope)}")
            return {"verdict": "fail", "covered": [], "missing": failed_in_scope,
                    "detail": f"생성된 테스트({SCENARIO_TEST_FILE})가 실행됐지만 실패했습니다:\n{detail}",
                    "group_titles": group_titles}

        if attempt < MAX_QA_BUILD_FIX_ROUNDS:
            print(f"[qa] 생성된 테스트 컴파일 실패 — 자체 수정 재시도 {attempt + 1}/{MAX_QA_BUILD_FIX_ROUNDS}")
            await emit_phase(r, project_id, stage, phase, "fail",
                              f"컴파일 실패 — 자체 수정 재시도 {attempt + 1}/{MAX_QA_BUILD_FIX_ROUNDS}")
            messages.append({"role": "assistant", "content": resp.content[0].text})
            fix_scope = "그 group(...) 블록 하나만" if response_groups is not None else "파일 전체를"
            messages.append({"role": "user", "content": (
                f"방금 작성한 테스트 코드가 컴파일에 실패했습니다:\n{detail}\n\n"
                f"이 컴파일 에러만 정확히 고치고, 나머지 시나리오/로직은 그대로 유지한 채 "
                f"{fix_scope} 다시 ```dart 코드 블록 하나에 출력하세요."
            )})
            continue

        # 자체 수정 예산을 다 썼는데도 컴파일이 안 됨 — Implement에 넘겨봐야
        # 소용없는 QA 자신의 코드 문제이므로, manual_build_fix가 켜져 있으면
        # 사람(Claude Code 세션)에게 넘기고, 꺼져 있으면 이번 라운드는 건너뛴다
        # (needs_rework로 Implement를 재작업시키지 않는다 — 앱 코드는 이미 맞을
        # 수 있어서 무한 루프만 돈다).
        if manual_build_fix:
            await emit_phase(r, project_id, stage, "scenario_fix", "fail",
                              f"자체 수정 {MAX_QA_BUILD_FIX_ROUNDS}회 소진 — 외부 처리로 넘김")
            return {"verdict": "manual_pending", "code": full_source, "titles": titles_this_attempt,
                    "detail": f"테스트 코드({SCENARIO_TEST_FILE}) 컴파일 실패, 자체 수정 {MAX_QA_BUILD_FIX_ROUNDS}회 소진:\n{detail}"}
        await emit_phase(r, project_id, stage, "scenario_fix", "skip",
                          f"자체 수정 {MAX_QA_BUILD_FIX_ROUNDS}회 소진 — 이번 라운드 건너뜀")
        return {"verdict": "skip",
                "detail": f"테스트 코드({SCENARIO_TEST_FILE}) 컴파일 실패를 자체 수정 {MAX_QA_BUILD_FIX_ROUNDS}회 시도에도 못 고쳐 이번 라운드는 건너뜁니다:\n{detail}"}

    return {"verdict": "skip", "detail": "알 수 없는 상태 — 재시도 루프가 결과 없이 끝남"}


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
    manual_qa_build_fix = bool(task.get("manual_qa_build_fix", False))
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

    await emit_phase(r, project_id, stage, "workspace_setup", "start", f"branch={branch or 'main'}")
    os.makedirs(workspace, exist_ok=True)
    if not os.path.exists(f"{workspace}/.git"):
        repo_url = f"https://{GITHUB_TOKEN}@github.com/{github_repo}.git"
        cp = run(["git", "clone", repo_url, workspace], timeout=180)
        if cp.returncode != 0:
            await emit_phase(r, project_id, stage, "workspace_setup", "fail", f"clone 실패: {cp.stderr[:200]}")
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
        await emit_phase(r, project_id, stage, "workspace_setup", "fail",
                          f"체크아웃 실패({checkout_target}): {checkout.stderr[:200]}")
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"❌ 브랜치 체크아웃 실패({checkout_target}): {checkout.stderr[:300]}"})
        return
    await emit_phase(r, project_id, stage, "workspace_setup", "success", checkout_target)

    manifest = load_scenario_manifest(workspace)

    await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                    "content": "🔍 PM 요구사항 대비 시나리오 구현 여부 검증 중..."})
    scenario = await verify_scenarios(r, project_id, stage, workspace, context, instruction, manual_qa_build_fix)
    if scenario.get("verdict") == "manual_pending":
        # QA가 자기 테스트 코드의 컴파일 실패를 자체 수정 예산 안에 못 고쳤고,
        # manual_qa_build_fix가 켜져 있다 — Implement에 넘기지 않고(앱 코드
        # 문제가 아니므로) 사람(Claude Code 세션)에게 직접 넘긴다. stage_completed를
        # 안 보내고 그냥 리턴해서 qa 스테이지가 RUNNING 상태로 남아있게 하고,
        # 사람이 POST .../stage/qa/manual-result로 완료 보고할 때까지 기다린다 —
        # implement의 MANUAL_TASKS_DIR 우회와 동일한 파일 규칙을 그대로 재사용.
        manual_dir = "/workspace/manual_tasks"
        os.makedirs(manual_dir, exist_ok=True)
        task_path = f"{manual_dir}/{project_id}_qa_qa_{int(time.time())}.json"
        with open(task_path, "w") as f:
            json.dump({
                "project_id": project_id, "stage": "qa", "kind": "qa_build_fix",
                "instruction": instruction, "broken_code": scenario.get("code", ""),
                "titles": scenario.get("titles", []), "build_error": scenario.get("detail", ""),
                "workspace": workspace, "test_file": SCENARIO_TEST_FILE,
            }, f, ensure_ascii=False, indent=2)
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"🖐 QA 테스트 코드 컴파일 실패 — 외부 처리 대기 중: {task_path}"})
        return
    if scenario.get("verdict") == "fail":
        missing = scenario.get("missing", [])
        detail = scenario.get("detail", "")
        feedback = f"다음 시나리오가 실행 테스트({SCENARIO_TEST_FILE})에서 실패했습니다: {', '.join(missing)}\n{detail}"
        group_titles = scenario.get("group_titles", {})
        for title in missing:
            upsert_scenarios(manifest, [title], "fail", detail, group=group_titles.get(title, ""))
        save_scenario_manifest(workspace, manifest)
        git_commit_and_push(workspace, "qa: 시나리오 테스트 갱신 (실패 발견)", branch,
                             paths=[SCENARIO_MANIFEST_FILE, SCENARIO_TEST_FILE])
        await emit_phase(r, project_id, stage, "finalize", "fail", "시나리오 검증 실패")
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
        await emit_phase(r, project_id, stage, "device_verify", "start")
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": "📱 같은 시나리오 테스트를 실제 기기(Firebase Test Lab)에서 재확인합니다..."})
        instr_ok, instr_detail, instr_app_apk, instr_test_apk = build_instrumentation_apks(workspace)
        if not instr_ok:
            await emit_phase(r, project_id, stage, "device_verify", "skip",
                              f"확인용 빌드 실패 — 로컬 결과만으로 진행: {instr_detail[:200]}")
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
            # 예전엔 blocking run()이라 최대 15분(timeout=900)까지 웹 UI에 아무
            # 신호도 없이 조용했다 — "QA가 멈췄나?"의 실제 원인이었다. gcloud도
            # 표준출력을 계속 찍으므로(업로드/프로비저닝/실행 단계) run_streaming으로
            # 바꿔 flutter build와 동일한 하트비트를 흘려보낸다.
            instr_run = await run_streaming(instr_cmd, workspace, 900, r, project_id, "실기기 계측 테스트")
            instr_result = parse_gcloud_output(instr_run.stdout, instr_run.stderr)
            if instr_result["passed"]:
                await emit_phase(r, project_id, stage, "device_verify", "success", instr_result["summary"])
                await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                                "content": f"✅ 실기기 시나리오 테스트 통과: {instr_result['summary']}"})
            else:
                feedback = (
                    f"로컬 시뮬레이션(flutter test)은 통과했지만 실제 기기에서 같은 시나리오 테스트가 "
                    f"실패했습니다 — 실기기에서만 드러나는 문제(타이밍, 실제 렌더링, 플랫폼 채널 등)일 "
                    f"수 있습니다: {instr_result['summary']}"
                )
                await emit_phase(r, project_id, stage, "device_verify", "fail", instr_result["summary"])
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
        await emit_phase(r, project_id, stage, "build_apk", "skip", "Implement가 빌드한 APK 재사용")
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"📦 Implement가 빌드한 APK를 그대로 사용합니다 (재빌드 생략): {os.path.basename(apk_path)}"})
    else:
        await emit_phase(r, project_id, stage, "build_apk", "start", " ".join(build_cmd))
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
            await emit_phase(r, project_id, stage, "build_apk", "fail", error_excerpt[:200])
            await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                            "content": f"❌ APK 빌드 실패:\n{error_excerpt}"})
            await emit(r, {"type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME, "stage": stage,
                            "outputs": {"agent": AGENT_NAME, "passed": False, "needs_rework": True,
                                        "feedback": f"APK 빌드 실패. 이 에러를 고쳐서 다시 구현하세요:\n{error_excerpt}",
                                        "summary": "APK 빌드 실패"}})
            return

        apk_path = find_apk(workspace)
        if not apk_path:
            await emit_phase(r, project_id, stage, "build_apk", "fail", "빌드는 성공했지만 APK 파일을 찾지 못함")
            await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                            "content": "❌ 빌드된 APK를 찾지 못했습니다."})
            await emit(r, {"type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME, "stage": stage,
                            "outputs": {"agent": AGENT_NAME, "passed": False, "needs_rework": True,
                                        "feedback": "빌드는 성공했지만 build/app/outputs/flutter-apk/ 안에서 APK 파일을 찾지 못했습니다.",
                                        "summary": "APK 없음"}})
            return
        await emit_phase(r, project_id, stage, "build_apk", "success", os.path.basename(apk_path))

    await emit_phase(r, project_id, stage, "robo_test", "start", os.path.basename(apk_path))
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
    # instrumentation 테스트와 같은 이유로 heartbeat가 있는 run_streaming을 쓴다 —
    # Robo 테스트도 몇 분씩 걸리는데 blocking run()은 끝날 때까지 웹에 아무
    # 신호도 안 보내서 "멈춘 것 아니냐"는 오해의 실제 원인이었다.
    test_run = await run_streaming(cmd, workspace, 900, r, project_id, "Robo 테스트")

    result = parse_gcloud_output(test_run.stdout, test_run.stderr)
    await emit_phase(r, project_id, stage, "robo_test", "success" if result["passed"] else "fail", result["summary"])
    await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                    "content": f"{'✅' if result['passed'] else '❌'} Test Lab 결과: {result['summary']}"})

    video_path = f"{output_dir}/qa_recording.mp4"
    video_ok = False
    design_mismatch_feedback = None
    if result["gcs_path"]:
        await emit_phase(r, project_id, stage, "result_download", "start")
        video_ok = download_video(result["gcs_path"], video_path)
        await emit_phase(r, project_id, stage, "result_download", "success" if video_ok else "fail",
                          "" if video_ok else "video.mp4 다운로드/재인코딩 실패")

        design_summary = ""
        if isinstance(context.get("design"), dict):
            design_summary = str(context["design"].get("summary", ""))
        if design_summary:
            await emit_phase(r, project_id, stage, "design_qa", "start")
            screenshots = download_screenshots(result["gcs_path"], f"{output_dir}/.qa_screenshots")
            design = await design_qa_check(project_id, screenshots, design_summary)
            verdict = design.get("verdict")
            if verdict == "mismatch":
                design_mismatch_feedback = (
                    f"디자인 QA — 실제 화면이 Designer 스펙과 다릅니다: {design.get('detail', '')}\n"
                    "다른 기능(카운터 로직 등)은 이미 정상이니, 스펙에 명시된 레이아웃/색상/컴포넌트만 맞춰주세요."
                )
                await emit_phase(r, project_id, stage, "design_qa", "fail", design.get("detail", ""))
                await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                                "content": f"🎨⚠️ 디자인 QA — 스펙과 실제 화면이 다릅니다. Implement에 재작업을 요청합니다:\n{design.get('detail', '')}"})
            elif verdict == "match":
                await emit_phase(r, project_id, stage, "design_qa", "success", design.get("detail", ""))
                await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                                "content": f"🎨✅ 디자인 QA 통과: {design.get('detail', '')}"})
            else:
                # unclear/skip도 조용히 넘어가지 않고 남긴다 — 안 그러면 디자인
                # QA가 아예 안 돌았는지, 판단을 못 한 건지 구분이 안 된다.
                await emit_phase(r, project_id, stage, "design_qa", "skip", f"{verdict or 'skip'}: {design.get('detail', '')}")
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
        group_titles = scenario.get("group_titles", {})
        for title in new_scenarios:
            upsert_scenarios(manifest, [title], status, note, group=group_titles.get(title, ""))
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
    await emit_phase(r, project_id, stage, "finalize", "success" if outputs["passed"] else "fail", outputs["summary"])
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
