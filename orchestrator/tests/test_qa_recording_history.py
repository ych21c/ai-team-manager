"""
회귀/기능 테스트 — QA 녹화 영상을 스프린트별로 남겨서(_archive_qa_recording)
웹 산출물 패널(collect_outputs)과 Jira 링크 양쪽에서 지난 라운드 영상도 계속
열어볼 수 있는지 확인한다.

배경: qa_recording.mp4는 QA가 돌 때마다 같은 경로에 덮어써진다. 지금까지는
Jira에 매번 파일을 통째로 첨부해서만 이전 라운드 영상을 보존했는데(웹에서는
못 봄), 사용자가 "이슈에 링크를 남기고 웹에서도 확인할 수 있게, 스프린트
기반으로" 요청해서 스프린트 태그가 붙은 고정 파일로 먼저 복사해두고 그
버전을 링크/노출하는 방식으로 바꿨다(publish_design의 design/history와
동일한 패턴).

실행: cd orchestrator && pytest tests/test_qa_recording_history.py -v
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _archive_qa_recording, collect_outputs

PID = "test-qa-recording-history"
WORKSPACE = f"/workspace/{PID}"


def _cleanup():
    shutil.rmtree(WORKSPACE, ignore_errors=True)


# ── _archive_qa_recording ────────────────────────────────────────────

def test_returns_none_when_no_recording_exists():
    _cleanup()
    try:
        assert _archive_qa_recording(PID, sprint=1, ts="20260818T120000") is None
    finally:
        _cleanup()


def test_copies_recording_to_sprint_tagged_history_file():
    _cleanup()
    try:
        os.makedirs(WORKSPACE, exist_ok=True)
        with open(f"{WORKSPACE}/qa_recording.mp4", "wb") as f:
            f.write(b"fake video bytes")

        version = _archive_qa_recording(PID, sprint=3, ts="20260818T120000")

        assert version == "sprint3_20260818T120000"
        hist_path = f"{WORKSPACE}/qa/history/{version}.mp4"
        assert os.path.exists(hist_path)
        with open(hist_path, "rb") as f:
            assert f.read() == b"fake video bytes"
        # 최신 qa_recording.mp4 자체는 그대로 남아있어야 함(/recordings/{pid}가 계속 서빙)
        assert os.path.exists(f"{WORKSPACE}/qa_recording.mp4")
    finally:
        _cleanup()


def test_multiple_rounds_keep_separate_history_files():
    """다음 라운드가 qa_recording.mp4를 덮어써도, 이전 라운드에 이미 만든
    이력 파일은 그대로 남아있어야 한다."""
    _cleanup()
    try:
        os.makedirs(WORKSPACE, exist_ok=True)
        with open(f"{WORKSPACE}/qa_recording.mp4", "wb") as f:
            f.write(b"round 1")
        v1 = _archive_qa_recording(PID, sprint=1, ts="20260818T100000")

        with open(f"{WORKSPACE}/qa_recording.mp4", "wb") as f:
            f.write(b"round 2")
        v2 = _archive_qa_recording(PID, sprint=1, ts="20260818T110000")

        assert v1 != v2
        assert os.path.exists(f"{WORKSPACE}/qa/history/{v1}.mp4")
        assert os.path.exists(f"{WORKSPACE}/qa/history/{v2}.mp4")
        with open(f"{WORKSPACE}/qa/history/{v1}.mp4", "rb") as f:
            assert f.read() == b"round 1"
    finally:
        _cleanup()


# ── collect_outputs — 스프린트별 QA 녹화 이력 노출 ────────────────────

def test_collect_outputs_includes_sprint_tagged_history(tmp_path):
    hist_dir = tmp_path / "qa" / "history"
    hist_dir.mkdir(parents=True)
    (hist_dir / "sprint2_20260818T120000.mp4").write_bytes(b"fake")

    items = collect_outputs(str(tmp_path), "p1")

    assert len(items) == 1
    assert items[0]["type"] == "video"
    assert items[0]["label"] == "QA 녹화 (Sprint 2)"
    assert items[0]["url"] == "/recordings/p1/history/sprint2_20260818T120000"


def test_collect_outputs_includes_both_latest_and_history_video(tmp_path):
    (tmp_path / "qa_recording.mp4").write_bytes(b"latest")
    hist_dir = tmp_path / "qa" / "history"
    hist_dir.mkdir(parents=True)
    (hist_dir / "sprint1_20260818T090000.mp4").write_bytes(b"old")

    items = collect_outputs(str(tmp_path), "p1")

    urls = {i["url"] for i in items}
    assert "/recordings/p1" in urls
    assert "/recordings/p1/history/sprint1_20260818T090000" in urls
    assert len(items) == 2


def test_collect_outputs_falls_back_to_raw_version_label_for_unexpected_filenames():
    """sprint{N}_ 패턴이 아닌 파일명이 섞여 있어도(수동으로 넣은 파일 등)
    죽지 않고 파일명을 그대로 라벨에 써야 한다."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        hist_dir = os.path.join(tmp, "qa", "history")
        os.makedirs(hist_dir)
        with open(os.path.join(hist_dir, "manual_upload.mp4"), "wb") as f:
            f.write(b"fake")

        items = collect_outputs(tmp, "p1")

        assert len(items) == 1
        assert items[0]["label"] == "QA 녹화 (manual_upload)"
