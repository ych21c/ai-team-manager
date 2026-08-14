"""
회귀 테스트 — 시나리오 검증용 소스 발췌가 파일당 400자로 무조건 잘려서, 정상
구현(lib/main.dart, ~950자)의 클래스 본문이 잘린 채로 LLM에 전달되던 사고.
counter-app에서 실제로 재현됨: LLM이 "createState()/State 클래스 구현부가 없다"고
오판해서 정상 코드를 needs_rework로 되돌려보냈다. 실제로는 그냥 화면에 안
보였을 뿐 코드에는 다 있었다.

이후 파일당 자르기 자체가 완전히 제거되고(run.py 참고) 전체 합계 기준
SOURCE_TOTAL_EXCERPT_LIMIT만 남았다 — 아래 테스트도 그에 맞춰 갱신.

실행: cd agents/qa_testlab && pip install pytest && pytest test_source_excerpt.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run import _list_source_files, SOURCE_TOTAL_EXCERPT_LIMIT


def _write_main_dart(tmp_path, body: str):
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "main.dart").write_text(body)


def test_small_file_is_not_cut_mid_class(tmp_path):
    """counter-app 재현 케이스: 400자보다 길지만(그때 기준으로 잘렸음)
    현재 한도보다는 짧은 전형적인 단일 화면 카운터 앱 — 끝까지 다 보여야 한다."""
    body = (
        "class CounterPage extends StatefulWidget {\n"
        "  const CounterPage({super.key});\n"
        "  @override\n"
        "  State<CounterPage> createState() => _CounterPageState();\n"
        "}\n\n"
        "class _CounterPageState extends State<CounterPage> {\n"
        "  int _counter = 0;\n"
        + "  // padding\n" * 60  # 400자를 넘기되 현재 한도보다는 짧게
        + "}\n"
    )
    assert len(body) > 400
    _write_main_dart(tmp_path, body)

    excerpt = _list_source_files(str(tmp_path))

    assert "createState() => _CounterPageState();" in excerpt
    assert "class _CounterPageState extends State<CounterPage>" in excerpt
    assert "이하 생략" not in excerpt


def test_actually_truncated_file_is_marked_as_truncated(tmp_path):
    """전체 합계가 SOURCE_TOTAL_EXCERPT_LIMIT을 넘는 비정상적으로 큰 파일은
    여전히 잘리지만, 잘렸다는 표시가 반드시 있어야 LLM이 \"화면에 안 보임\"과
    \"실제로 없음\"을 구분할 수 있다."""
    body = "x = 1\n" * (SOURCE_TOTAL_EXCERPT_LIMIT // 6 + 200)
    _write_main_dart(tmp_path, body)

    excerpt = _list_source_files(str(tmp_path))

    assert "이하 생략" in excerpt


def test_no_lib_dir_reports_clearly(tmp_path):
    excerpt = _list_source_files(str(tmp_path))
    assert "없음" in excerpt
