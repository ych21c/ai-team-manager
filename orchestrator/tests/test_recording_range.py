"""
회귀 테스트 — QA 녹화 영상이 맥(Chrome/Safari)에서는 재생되는데 모바일
Chrome에서는 재생이 안 되던 사고. 모바일 Chrome의 <video>는 재생/탐색 전에
반드시 HTTP Range 요청을 보내고 206 Partial Content로 응답하지 않으면 재생을
거부하는데, Starlette FileResponse는 Range를 전혀 처리하지 않고 항상 200 +
전체 파일만 돌려준다 — 데스크톱 브라우저는 그래도 관대하게 재생해버려서
모바일에서 실제로 겪기 전까진 드러나지 않았다.

실행: cd orchestrator && pytest tests/test_recording_range.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from fastapi.responses import FileResponse, StreamingResponse


async def _drain(response: StreamingResponse) -> bytes:
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else chunk.encode()
    return body


@pytest.fixture
def video_file(tmp_path):
    content = bytes(range(256)) * 4  # 1024바이트, 오프셋 검증하기 쉬운 패턴
    path = tmp_path / "qa_recording.mp4"
    path.write_bytes(content)
    return str(path), content


# ── _parse_byte_range (순수 함수) ────────────────────────────────────

def test_no_range_header_returns_none():
    assert main._parse_byte_range(None, 1000) is None
    assert main._parse_byte_range("", 1000) is None


def test_simple_range_parsed():
    assert main._parse_byte_range("bytes=0-3", 1000) == (0, 3)


def test_open_ended_range_uses_file_size_minus_one():
    assert main._parse_byte_range("bytes=990-", 1000) == (990, 999)


def test_suffix_range_counts_from_end():
    assert main._parse_byte_range("bytes=-100", 1000) == (900, 999)


def test_end_beyond_file_size_is_clamped():
    assert main._parse_byte_range("bytes=0-99999", 1000) == (0, 999)


def test_start_at_or_past_file_size_returns_none():
    assert main._parse_byte_range("bytes=1000-", 1000) is None
    assert main._parse_byte_range("bytes=5000-6000", 1000) is None


def test_malformed_header_returns_none():
    assert main._parse_byte_range("not-a-range", 1000) is None
    assert main._parse_byte_range("bytes=", 1000) is None


def test_multi_range_header_only_uses_first_range():
    assert main._parse_byte_range("bytes=0-1,10-20", 1000) == (0, 1)


# ── _serve_video_range (실제 파일 I/O + 응답 조립) ────────────────────

@pytest.mark.asyncio
async def test_no_range_header_falls_back_to_full_file_response(video_file):
    path, _ = video_file
    resp = main._serve_video_range(path, None)
    assert isinstance(resp, FileResponse)
    assert resp.headers["accept-ranges"] == "bytes"


@pytest.mark.asyncio
async def test_range_request_returns_206_with_correct_slice(video_file):
    path, content = video_file
    resp = main._serve_video_range(path, "bytes=10-19")
    assert isinstance(resp, StreamingResponse)
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 10-19/{len(content)}"
    assert resp.headers["content-length"] == "10"
    assert resp.headers["accept-ranges"] == "bytes"
    body = await _drain(resp)
    assert body == content[10:20]


@pytest.mark.asyncio
async def test_range_request_near_end_of_file_returns_tail_bytes(video_file):
    path, content = video_file
    resp = main._serve_video_range(path, "bytes=-16")
    body = await _drain(resp)
    assert body == content[-16:]
    assert resp.headers["content-range"] == f"bytes {len(content) - 16}-{len(content) - 1}/{len(content)}"


@pytest.mark.asyncio
async def test_invalid_range_falls_back_to_full_file_response(video_file):
    path, _ = video_file
    resp = main._serve_video_range(path, "garbage")
    assert isinstance(resp, FileResponse)
