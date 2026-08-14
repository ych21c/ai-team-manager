"""
회귀 테스트 — Firebase Test Lab이 주는 qa_recording.mp4가 실제로는 VP9 스트림을
MP4 컨테이너에 담고 있어서(ffprobe로 실측: codec_tag_string=vp09), 맥
Chrome/Safari에서는 재생되지만 모바일 Chrome의 <video> progressive playback은
거부하던 사고. 다운로드 직후 H.264/AAC로 재인코딩해서 저장하도록 고쳤다.

실행: cd agents/qa_testlab && pytest test_video_transcode.py -v
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run as run_module
from run import _ffmpeg_transcode_cmd, _transcode_to_h264, download_video


def _fake_completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ── _ffmpeg_transcode_cmd (순수 함수) ────────────────────────────────

def test_transcode_cmd_uses_h264_aac_and_faststart():
    cmd = _ffmpeg_transcode_cmd("/tmp/raw.mp4", "/tmp/out.mp4")
    assert cmd[0] == "ffmpeg"
    assert "/tmp/raw.mp4" in cmd
    assert cmd[-1] == "/tmp/out.mp4"
    assert "libx264" in cmd
    assert "aac" in cmd
    assert "+faststart" in cmd


def test_transcode_cmd_overwrites_without_prompting():
    # -y 없으면 ffmpeg가 dest_path 이미 있을 때 대화형으로 덮어쓸지 물어봐서
    # subprocess가 멈춰버린다 — 절대 빠지면 안 되는 플래그.
    assert "-y" in _ffmpeg_transcode_cmd("/tmp/a.mp4", "/tmp/b.mp4")


# ── _transcode_to_h264 ────────────────────────────────────────────────

def test_transcode_success_returns_true(tmp_path, monkeypatch):
    dest = tmp_path / "out.mp4"

    def fake_run(cmd, cwd=None, timeout=60):
        dest.write_bytes(b"fake h264 bytes")  # ffmpeg가 실제로 만들었다고 가정
        return _fake_completed(returncode=0)

    monkeypatch.setattr(run_module, "run", fake_run)
    assert _transcode_to_h264(str(tmp_path / "raw.mp4"), str(dest)) is True


def test_transcode_nonzero_exit_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(run_module, "run", lambda *a, **k: _fake_completed(returncode=1, stderr="boom"))
    assert _transcode_to_h264(str(tmp_path / "raw.mp4"), str(tmp_path / "out.mp4")) is False


def test_transcode_success_exit_but_no_output_file_returns_false(tmp_path, monkeypatch):
    # returncode=0인데 결과 파일이 없거나 0바이트면(이상 종료 등) 성공으로 치지 않는다.
    monkeypatch.setattr(run_module, "run", lambda *a, **k: _fake_completed(returncode=0))
    assert _transcode_to_h264(str(tmp_path / "raw.mp4"), str(tmp_path / "missing.mp4")) is False


# ── download_video (gcloud cp + 재인코딩 orchestration) ────────────────

def test_download_video_transcodes_and_cleans_up_raw_file(tmp_path, monkeypatch):
    dest = tmp_path / "qa_recording.mp4"
    raw = tmp_path / "qa_recording.mp4.raw.mp4"

    calls = []

    def fake_run(cmd, cwd=None, timeout=60):
        calls.append(cmd)
        if cmd[:2] == ["gcloud", "storage"] and cmd[2] == "ls":
            return _fake_completed(stdout="gs://bucket/run/device/artifacts/video.mp4\n")
        if cmd[:3] == ["gcloud", "storage", "cp"]:
            raw.write_bytes(b"raw vp9 bytes")
            return _fake_completed(returncode=0)
        if cmd[0] == "ffmpeg":
            dest.write_bytes(b"transcoded h264 bytes")
            return _fake_completed(returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(run_module, "run", fake_run)
    assert download_video("gs://bucket/run", str(dest)) is True
    assert dest.read_bytes() == b"transcoded h264 bytes"
    assert not raw.exists()  # 재인코딩 성공하면 원본(VP9) 임시 파일은 지운다


def test_download_video_falls_back_to_raw_file_when_transcode_fails(tmp_path, monkeypatch):
    dest = tmp_path / "qa_recording.mp4"
    raw = tmp_path / "qa_recording.mp4.raw.mp4"

    def fake_run(cmd, cwd=None, timeout=60):
        if cmd[:2] == ["gcloud", "storage"] and cmd[2] == "ls":
            return _fake_completed(stdout="gs://bucket/run/device/artifacts/video.mp4\n")
        if cmd[:3] == ["gcloud", "storage", "cp"]:
            raw.write_bytes(b"raw vp9 bytes")
            return _fake_completed(returncode=0)
        if cmd[0] == "ffmpeg":
            return _fake_completed(returncode=1, stderr="ffmpeg missing or crashed")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(run_module, "run", fake_run)
    # ffmpeg가 없거나 실패해도 QA 자체는 막지 않는다 — 호환성 떨어지는 원본이라도
    # 서빙하는 게 영상이 아예 없는 것보다 낫다.
    assert download_video("gs://bucket/run", str(dest)) is True
    assert dest.read_bytes() == b"raw vp9 bytes"
    assert not raw.exists()


def test_download_video_returns_false_when_no_video_found(monkeypatch):
    monkeypatch.setattr(run_module, "run", lambda *a, **k: _fake_completed(stdout="gs://bucket/run/logcat\n"))
    assert download_video("gs://bucket/run", "/tmp/whatever.mp4") is False


def test_download_video_returns_false_when_gcs_cp_fails(tmp_path, monkeypatch):
    def fake_run(cmd, cwd=None, timeout=60):
        if cmd[:2] == ["gcloud", "storage"] and cmd[2] == "ls":
            return _fake_completed(stdout="gs://bucket/run/device/artifacts/video.mp4\n")
        return _fake_completed(returncode=1)

    monkeypatch.setattr(run_module, "run", fake_run)
    assert download_video("gs://bucket/run", str(tmp_path / "out.mp4")) is False
