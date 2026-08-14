"""
회귀 테스트 — design 스테이지가 시나리오(Jira 스토리)별로 목업을 나눠 관리하는
_list_design_bucket / _safe_design_key. PR 생성·머지(GitHub API 호출)는 여기서
목킹하지 않고, 파일 나열·경로탈출 방어처럼 순수 로직만 다룬다.

실행: cd orchestrator && pytest tests/test_design_publish.py -v
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _list_design_bucket, _safe_design_key


def test_safe_design_key_rejects_path_traversal():
    assert _safe_design_key("ATM-2")
    assert not _safe_design_key("../../etc/passwd")
    assert not _safe_design_key("a/b")
    assert not _safe_design_key("")


def test_list_design_bucket_empty_when_dir_missing(tmp_path):
    assert _list_design_bucket(str(tmp_path), "p1", "applied") == []


def test_list_design_bucket_labels_with_jira_story_title(tmp_path):
    applied_dir = tmp_path / "design" / "applied"
    applied_dir.mkdir(parents=True)
    (applied_dir / "ATM-2.html").write_text("<html>login</html>")

    items = _list_design_bucket(str(tmp_path), "p1", "applied", {"ATM-2": "로그인 화면"})
    assert len(items) == 1
    assert items[0]["key"] == "ATM-2"
    assert items[0]["title"] == "로그인 화면"
    assert items[0]["url"] == "/design-file/p1/applied/ATM-2"


def test_list_design_bucket_falls_back_to_key_without_title(tmp_path):
    applied_dir = tmp_path / "design" / "applied"
    applied_dir.mkdir(parents=True)
    (applied_dir / "main.html").write_text("<html></html>")

    items = _list_design_bucket(str(tmp_path), "p1", "applied")
    assert items[0]["title"] == "main"


def test_list_design_bucket_sorted_newest_first(tmp_path):
    applied_dir = tmp_path / "design" / "applied"
    applied_dir.mkdir(parents=True)
    (applied_dir / "ATM-2.html").write_text("old")
    old_time = time.time() - 100
    os.utime(applied_dir / "ATM-2.html", (old_time, old_time))

    time.sleep(0.01)
    (applied_dir / "ATM-3.html").write_text("new")

    items = _list_design_bucket(str(tmp_path), "p1", "applied")
    assert [i["key"] for i in items] == ["ATM-3", "ATM-2"]
