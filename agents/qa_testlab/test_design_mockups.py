"""
회귀 테스트 — verify_scenarios()가 시나리오 테스트를 짤 때 Designer의 텍스트
요약(summary) 대신/추가로 실제 HTML 목업(design/applied 또는 design/pending)을
그대로 참고하도록 바꾼 부분. 텍스트 요약은 사람이 다시 압축한 설명이라 정확한
문구/버튼 라벨이 요약 과정에서 빠지기 쉽다는 지적(2026-08-15)에 따라 추가됨.

실행: cd agents/qa_testlab && pytest test_design_mockups.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run import _list_design_mockups, MOCKUP_TOTAL_EXCERPT_LIMIT


def test_prefers_applied_over_pending(tmp_path):
    """머지된 뒤(design/applied)엔 그게 최신본이므로, pending에 옛 버전이 남아
    있어도 applied를 우선해야 한다."""
    applied_dir = tmp_path / "design" / "applied"
    pending_dir = tmp_path / "design" / "pending"
    applied_dir.mkdir(parents=True)
    pending_dir.mkdir(parents=True)
    (applied_dir / "ATM-5.html").write_text("<button>최신 버전</button>")
    (pending_dir / "ATM-5.html").write_text("<button>옛 버전</button>")

    excerpt = _list_design_mockups(str(tmp_path))

    assert "최신 버전" in excerpt
    assert "옛 버전" not in excerpt
    assert "design/applied/ATM-5.html" in excerpt


def test_falls_back_to_pending_when_not_yet_merged(tmp_path):
    pending_dir = tmp_path / "design" / "pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / "ATM-5.html").write_text("<button>대기 중</button>")

    excerpt = _list_design_mockups(str(tmp_path))

    assert "대기 중" in excerpt
    assert "design/pending/ATM-5.html" in excerpt


def test_multiple_scenarios_all_included(tmp_path):
    applied_dir = tmp_path / "design" / "applied"
    applied_dir.mkdir(parents=True)
    (applied_dir / "ATM-5.html").write_text("<button>화면1</button>")
    (applied_dir / "ATM-6.html").write_text("<button>화면2</button>")

    excerpt = _list_design_mockups(str(tmp_path))

    assert "화면1" in excerpt
    assert "화면2" in excerpt


def test_no_mockup_dir_reports_clearly(tmp_path):
    excerpt = _list_design_mockups(str(tmp_path))
    assert "없음" in excerpt


def test_dir_exists_but_no_html_files_reports_clearly(tmp_path):
    (tmp_path / "design" / "applied").mkdir(parents=True)
    excerpt = _list_design_mockups(str(tmp_path))
    assert "없음" in excerpt


def test_oversized_total_is_truncated_and_marked(tmp_path):
    applied_dir = tmp_path / "design" / "applied"
    applied_dir.mkdir(parents=True)
    (applied_dir / "huge.html").write_text("x" * (MOCKUP_TOTAL_EXCERPT_LIMIT + 1000))

    excerpt = _list_design_mockups(str(tmp_path))

    assert "이하 생략" in excerpt
