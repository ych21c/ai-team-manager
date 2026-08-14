"""
회귀/기능 테스트 — 프로젝트 산출물(디자인 목업/QA 녹화영상/스크린샷)을 최신순으로
모아주는 collect_outputs(). 사용자 요청: "산출물들(웹, 영상, 사진 등)은 최신순으로
보여주고 눌러서 페이지로 갈 수 있는 버튼이 있었으면 좋겠다."

실행: cd orchestrator && pytest tests/test_outputs.py -v
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import collect_outputs


def test_empty_dir_returns_no_items(tmp_path):
    assert collect_outputs(str(tmp_path), "p1") == []


def test_video_included_with_correct_url(tmp_path):
    (tmp_path / "qa_recording.mp4").write_bytes(b"fake")
    items = collect_outputs(str(tmp_path), "p1")
    assert len(items) == 1
    assert items[0]["type"] == "video"
    assert items[0]["url"] == "/recordings/p1"


def test_screenshots_included_with_per_file_urls(tmp_path):
    shots = tmp_path / ".qa_screenshots"
    shots.mkdir()
    (shots / "screenshot_0.png").write_bytes(b"fake")
    (shots / "screenshot_1.png").write_bytes(b"fake")
    (shots / "not_an_image.txt").write_text("skip me")

    items = collect_outputs(str(tmp_path), "p1")
    urls = {i["url"] for i in items}
    assert "/screenshots/p1/screenshot_0.png" in urls
    assert "/screenshots/p1/screenshot_1.png" in urls
    assert len(items) == 2  # .txt 파일은 제외


def test_items_sorted_newest_first(tmp_path):
    shots = tmp_path / ".qa_screenshots"
    shots.mkdir()
    (shots / "screenshot_0.png").write_bytes(b"old")
    old_time = time.time() - 100
    os.utime(shots / "screenshot_0.png", (old_time, old_time))

    time.sleep(0.01)
    (tmp_path / "qa_recording.mp4").write_bytes(b"new")

    items = collect_outputs(str(tmp_path), "p1")
    assert [i["type"] for i in items] == ["video", "screenshot"]
