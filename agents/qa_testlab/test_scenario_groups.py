"""
회귀 테스트 — scenario_test.dart를 화면/기능 단위 group(...) 블록으로 쪼개고
다시 합치는 파서(parse_scenario_groups/rebuild_scenario_test/_merge_groups/
_extract_response_groups). QA가 매 라운드 파일 전체를 처음부터 새로 쓰는 대신,
이번 라운드가 가리키는 그룹만 잘라서 LLM에 넘기고 나머지 그룹은 파이썬이
그대로 보존하도록 바꾼 부분(2026-08-21)의 핵심 로직이라 별도 회귀 테스트로
고정해둔다.

실행: cd agents/qa_testlab && pytest test_scenario_groups.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run import (
    _extract_response_groups,
    _find_matching_brace,
    _merge_groups,
    _split_group_blocks,
    _TESTWIDGETS_TITLE_RE,
    parse_scenario_groups,
    rebuild_scenario_test,
)

SAMPLE_FILE = """import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

import 'package:demo/app.dart';

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  group('시작 페이지', () {
    testWidgets('스플래시 화면 표시', (tester) async {
      await tester.pumpWidget(const DemoApp());
      expect(find.text('Demo'), findsWidgets);
    });

    testWidgets('랜딩 화면 CTA 버튼', (tester) async {
      expect(find.text('시작하기'), findsOneWidget);
    });
  });

  group('결제 화면', () {
    testWidgets('결제 버튼 표시', (tester) async {
      expect(find.text('결제'), findsOneWidget);
    });
  });
}
"""


def test_parse_finds_all_groups_and_titles():
    parsed = parse_scenario_groups(SAMPLE_FILE)
    assert parsed is not None
    names = [name for name, _ in parsed["groups"]]
    assert names == ["시작 페이지", "결제 화면"]

    titles_by_group = {
        name: _TESTWIDGETS_TITLE_RE.findall(block) for name, block in parsed["groups"]
    }
    assert titles_by_group["시작 페이지"] == ["스플래시 화면 표시", "랜딩 화면 CTA 버튼"]
    assert titles_by_group["결제 화면"] == ["결제 버튼 표시"]


def test_parse_returns_none_for_unrecognized_shape():
    assert parse_scenario_groups("this is not dart at all") is None
    assert parse_scenario_groups("void main() {\n  print('no groups here');\n}") is None


def test_brace_matching_ignores_braces_in_strings_and_comments():
    src = "group('a', () {\n  // a { fake brace\n  var s = '{not a brace}';\n});"
    open_idx = src.index("{")
    close = _find_matching_brace(src, open_idx)
    # 문자열/주석 안의 '{'/'}'는 안 세므로, 콜백을 닫는 진짜 '}'(뒤에 ");"가
    # 바로 이어짐)를 찾아야 한다 — 순진하게 다 세면 훨씬 앞에서 잘못 닫힌다.
    assert src[close] == "}"
    assert src[close:] == "});"


def test_split_group_blocks_skips_nested_scan_after_each_block():
    body = "\n  group('a', () { testWidgets('x', (t) async {}); });\n\n  group('b', () { testWidgets('y', (t) async {}); });\n"
    blocks = _split_group_blocks(body)
    assert [name for name, _, _ in blocks] == ["a", "b"]


def test_rebuild_roundtrip_preserves_group_content():
    parsed = parse_scenario_groups(SAMPLE_FILE)
    rebuilt = rebuild_scenario_test(parsed)
    reparsed = parse_scenario_groups(rebuilt)
    assert reparsed is not None
    assert [n for n, _ in reparsed["groups"]] == ["시작 페이지", "결제 화면"]
    # Dart는 공백에 의미가 없으므로 정규화(공백 제거) 후 텍스트가 같은지만 비교.
    for (n1, b1), (n2, b2) in zip(parsed["groups"], reparsed["groups"]):
        assert n1 == n2
        assert "".join(b1.split()) == "".join(b2.split())


def test_extract_response_groups_single_block():
    response = """group('시작 페이지', () {
  testWidgets('새로 고친 시나리오', (tester) async {
    expect(1, 1);
  });
});"""
    groups = _extract_response_groups(response)
    assert groups is not None
    assert len(groups) == 1
    name, block = groups[0]
    assert name == "시작 페이지"
    assert "새로 고친 시나리오" in block


def test_extract_response_groups_tolerates_whole_file_response():
    # LLM이 "그룹 블록만 출력하세요" 지시를 무시하고 파일 전체(임포트+void
    # main() 포함)를 그대로 냈더라도, void main() {...} 껍데기로 다시 감싸는
    # 트릭 덕분에 임포트/void main은 그냥 "그룹 이전 잡음"으로 무시되고 실제
    # group(...) 블록들은 그대로 뽑힌다 — 별도 폴백 분기 없이도 관대하게 처리.
    groups = _extract_response_groups(SAMPLE_FILE)
    assert groups is not None
    assert [name for name, _ in groups] == ["시작 페이지", "결제 화면"]


def test_merge_groups_replaces_matching_name_and_appends_new():
    existing = [("시작 페이지", "group('시작 페이지', () { testWidgets('old', (t) async {}); });"),
                ("결제 화면", "group('결제 화면', () { testWidgets('pay', (t) async {}); });")]
    updated = [("시작 페이지", "group('시작 페이지', () { testWidgets('new', (t) async {}); });")]

    merged = _merge_groups(existing, updated)
    assert [name for name, _ in merged] == ["시작 페이지", "결제 화면"]
    assert "new" in dict(merged)["시작 페이지"]
    assert "pay" in dict(merged)["결제 화면"]  # 안 건드린 그룹은 그대로

    brand_new = [("알림 화면", "group('알림 화면', () { testWidgets('noti', (t) async {}); });")]
    merged2 = _merge_groups(existing, brand_new)
    assert [name for name, _ in merged2] == ["시작 페이지", "결제 화면", "알림 화면"]
