"""
회귀/기능 테스트 — QA 항목에서 flutter build 같은 오래 걸리는 명령이 끝날 때까지
몇 분간 아무 로그도 안 보여서 "멈춘 것처럼" 보이던 문제. run_streaming이 진행
중에도 주기적으로 message 이벤트를 emit하는지, ANSI/스피너 프레임을 걸러내는지,
최종 결과(returncode/stdout)가 기존 run()과 호환되는지 확인한다.

실행: cd agents/qa_testlab && pip install pytest pytest-asyncio && pytest test_run_streaming.py -v
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run import run_streaming, _flush_build_progress

pytestmark = pytest.mark.asyncio


class FakeRedis:
    """emit()이 쓰는 xadd만 흉내내고, 보낸 이벤트를 리스트에 그대로 쌓아서
    검증할 수 있게 한다 — 진짜 Redis 없이도 emit 흐름을 테스트하기 위함."""
    def __init__(self):
        self.sent = []

    async def xadd(self, stream, fields):
        self.sent.append(json.loads(fields["payload"]))


async def test_emits_progress_while_long_command_runs():
    r = FakeRedis()
    cmd = ["bash", "-c", "echo start; sleep 0.3; echo middle; sleep 0.3; echo done"]

    result = await run_streaming(cmd, cwd=".", timeout=10, r=r, project_id="p1",
                                  label="테스트 빌드", interval=0.1)

    assert result.returncode == 0
    assert "start" in result.stdout
    assert "middle" in result.stdout
    assert "done" in result.stdout
    # 최소 한 번은 중간에 진행 메시지가 나갔어야 한다 (다 끝난 뒤 한 번만이 아니라).
    assert len(r.sent) >= 1
    assert all(evt["type"] == "message" and evt["project_id"] == "p1" for evt in r.sent)
    assert any("테스트 빌드" in evt["content"] for evt in r.sent)


async def test_nonzero_exit_code_is_reported():
    r = FakeRedis()
    result = await run_streaming(["bash", "-c", "echo boom; exit 3"], cwd=".", timeout=10,
                                  r=r, project_id="p1", label="실패 테스트", interval=0.1)
    assert result.returncode == 3
    assert "boom" in result.stdout


async def test_timeout_raises_and_kills_process():
    r = FakeRedis()
    with pytest.raises(asyncio.TimeoutError):
        await run_streaming(["sleep", "5"], cwd=".", timeout=0.2, r=r,
                             project_id="p1", label="타임아웃 테스트", interval=0.05)


async def test_silent_command_still_gets_heartbeat_progress():
    """실제로 있었던 문제의 핵심 회귀 테스트: flutter build는 Gradle을 -q로
    감싸서 몇 분씩 stdout을 전혀 안 찍는 구간이 있는데, 그때도 사용자에게는
    "아직 살아있다"는 신호가 주기적으로 나가야 한다 — 로그가 하나도 없으면
    멈춘 것처럼 보인다는 사용자 피드백으로 발견된 버그."""
    r = FakeRedis()
    cmd = ["bash", "-c", "sleep 0.5"]  # 실행 내내 stdout에 아무 것도 안 씀

    result = await run_streaming(cmd, cwd=".", timeout=10, r=r, project_id="p1",
                                  label="조용한 빌드", interval=0.1)

    assert result.returncode == 0
    # 출력이 하나도 없었는데도 최소 한 번은 하트비트가 나갔어야 한다.
    assert len(r.sent) >= 1
    assert all("초 경과" in evt["content"] for evt in r.sent)


async def test_flush_strips_ansi_and_carriage_return_spinner_frames():
    r = FakeRedis()
    # Gradle/flutter 스피너가 실제로 내는 것과 비슷한 패턴: \x1b 색상 코드 +
    # \r로 덮어쓰는 중간 프레임들.
    buf = bytearray(b"\x1b[32mRunning Gradle task\x1b[0m...\r25%\r50%\r100%\nBuilt app-debug.apk\n")
    await _flush_build_progress(r, "p1", "APK 빌드", buf, elapsed=12)

    assert len(r.sent) == 1
    content = r.sent[0]["content"]
    assert "\x1b" not in content
    assert "Built app-debug.apk" in content
    assert "12초 경과" in content


async def test_flush_emits_heartbeat_even_on_empty_buffer():
    r = FakeRedis()
    await _flush_build_progress(r, "p1", "APK 빌드", bytearray(), elapsed=30)
    assert len(r.sent) == 1
    assert "30초 경과" in r.sent[0]["content"]
    assert "아직 출력 없음" in r.sent[0]["content"]
