"""
회귀 테스트 — add_jira_attachment()의 네트워크 호출 없는 방어 분기(인증 정보
없음 / 첨부할 파일이 실제로 없음)만 검증한다. 실제 Jira API 호출은 이
저장소의 다른 통합 지점과 마찬가지로 목킹하지 않는다.

실행: cd orchestrator && pytest tests/test_jira_attachment.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import atlassian_client

pytestmark = pytest.mark.asyncio


async def test_missing_credentials_returns_false_without_network_call(monkeypatch, tmp_path):
    monkeypatch.setattr(atlassian_client, "EMAIL", "")
    monkeypatch.setattr(atlassian_client, "TOKEN", "")
    monkeypatch.setattr(atlassian_client, "DOMAIN", "")
    f = tmp_path / "recording.mp4"
    f.write_bytes(b"video")

    assert await atlassian_client.add_jira_attachment("ATM-5", str(f)) is False


async def test_missing_file_returns_false(monkeypatch, tmp_path):
    monkeypatch.setattr(atlassian_client, "EMAIL", "bot@example.com")
    monkeypatch.setattr(atlassian_client, "TOKEN", "token")
    monkeypatch.setattr(atlassian_client, "DOMAIN", "example.atlassian.net")

    assert await atlassian_client.add_jira_attachment("ATM-5", str(tmp_path / "missing.mp4")) is False
