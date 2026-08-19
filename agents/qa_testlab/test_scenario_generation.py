"""
회귀 테스트 — PM/Designer 시나리오를 "코드를 읽고 판단"이 아니라 "테스트 코드를
생성해서 실제로 실행"하는 방식으로 검증하도록 바꾼 부분의 순수 로직(LLM 호출은
빼고 파싱/추출 유틸만) 테스트.

실행: cd agents/qa_testlab && pip install pytest && pytest test_scenario_generation.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run import (
    _pubspec_package_name, _DART_CODE_BLOCK_RE, _TESTWIDGETS_TITLE_RE,
    _filter_screenshot_paths, _extract_json_object, _finalize_qa_outputs,
    _resolve_target_branch, _extract_scenario_test_code, _scenario_test_cmd,
    _list_source_files, SOURCE_TOTAL_EXCERPT_LIMIT, determine_build_command,
    _VERIFY_SCENARIOS_RULES,
)


def test_rules_warn_against_pumpandsettle_with_repeating_animation():
    # recoveryfit(c052dd6b)에서 실제로 재현된 사고: 스플래시 화면의 로딩 도트가
    # AnimationController.repeat()으로 영원히 반복되는데, QA가 생성한 테스트가
    # 그 화면에서 pumpAndSettle()을 써서 — 앱 코드가 맞아도 테스트가 원천적으로
    # 통과 불가능한 상태로 몇 시간째 Implement에 "고쳐라"는 재작업 피드백만
    # 반복해서 보냈다. 이 규칙 문구가 없으면 같은 사고가 다른 프로젝트/화면에서
    # 재발한다.
    assert "pumpAndSettle" in _VERIFY_SCENARIOS_RULES
    assert ".repeat()" in _VERIFY_SCENARIOS_RULES
    assert "tester.pump(Duration(" in _VERIFY_SCENARIOS_RULES


def test_rules_warn_against_single_big_pump_for_async_gated_navigation():
    # recoveryfit(c052dd6b)에서 실제로 재현된 사고: 스플래시→랜딩 전환이 애니메이션뿐
    # 아니라 실제 비동기 초기화(Hive/알림 서비스)까지 끝나야 일어나는데, QA가 생성한
    # 테스트가 `tester.pump(Duration(seconds: 3))`처럼 한 번에 큰 Duration을 pump해서
    # (프레임/마이크로태스크가 한 번만 진행됨) 3초를 기다려도 여전히 스플래시 화면에
    # 머물러 있었다 — 앱은 정상인데 테스트가 원천적으로 통과 불가능해서 Implement에
    # 재작업 요청만 반복 소진했다.
    assert "pump(Duration(seconds: 3))" in _VERIFY_SCENARIOS_RULES
    assert "pumpUntilFound" in _VERIFY_SCENARIOS_RULES


def test_rules_warn_against_referencing_unimported_screen_classes():
    # recoveryfit에서 실제 재현: QA가 생성한 테스트가 `find.byType(LandingScreen)`을
    # 썼는데 그 클래스가 정의된 파일을 import하지 않아서(테스트는 main.dart만 import)
    # "Undefined name 'LandingScreen'"로 컴파일 자체가 실패했다.
    assert "find.byType(LandingScreen)" in _VERIFY_SCENARIOS_RULES
    assert "find.byType(SplashScreen)" in _VERIFY_SCENARIOS_RULES


def test_rules_warn_against_joined_text_when_source_splits_lines():
    # recoveryfit에서 실제 재현: 헤드라인이 소스에서 Text('첫줄')/Text('둘째줄')로 두
    # 위젯에 나뉘어 있는데, 생성된 테스트는 find.text('첫줄\\n둘째줄')로 합쳐서 찾아서
    # 항상 findsNothing으로 실패했다.
    assert "findsNothing" in _VERIFY_SCENARIOS_RULES
    assert "\\n" in _VERIFY_SCENARIOS_RULES


def test_pubspec_package_name_extracted(tmp_path):
    (tmp_path / "pubspec.yaml").write_text(
        "name: counter_app\ndescription: A new Flutter project.\n"
    )
    assert _pubspec_package_name(str(tmp_path)) == "counter_app"


def test_pubspec_package_name_none_when_missing(tmp_path):
    assert _pubspec_package_name(str(tmp_path)) is None


def test_dart_code_block_extracted_from_llm_response():
    response = (
        "여기 테스트 코드입니다:\n\n"
        "```dart\n"
        "import 'package:flutter_test/flutter_test.dart';\n"
        "void main() {}\n"
        "```\n"
        "이상입니다."
    )
    m = _DART_CODE_BLOCK_RE.search(response)
    assert m is not None
    code = m.group(1).strip()
    assert code.startswith("import 'package:flutter_test/flutter_test.dart';")
    assert "이상입니다" not in code


def test_testwidgets_titles_extracted():
    code = """
testWidgets('초기 카운터는 0을 표시한다', (tester) async {});
testWidgets("버튼을 누르면 1 증가한다", (tester) async {});
"""
    titles = _TESTWIDGETS_TITLE_RE.findall(code)
    assert titles == ["초기 카운터는 0을 표시한다", "버튼을 누르면 1 증가한다"]


def test_testwidgets_titles_empty_when_none_present():
    assert _TESTWIDGETS_TITLE_RE.findall("void main() {}") == []


def test_filter_screenshot_paths_picks_only_app_screens():
    # counter-app 실행에서 실제로 나온 gcloud storage ls -r 출력 형태 그대로.
    ls_output = """gs://bucket/run/MediumPhone.arm-34-en-portrait/actions.json
gs://bucket/run/MediumPhone.arm-34-en-portrait/artifacts/0.png
gs://bucket/run/MediumPhone.arm-34-en-portrait/artifacts/1.png
gs://bucket/run/MediumPhone.arm-34-en-portrait/artifacts/output/sitemap.png
gs://bucket/run/MediumPhone.arm-34-en-portrait/crawlscript.json
gs://bucket/run/MediumPhone.arm-34-en-portrait/video.mp4
gs://bucket/run/app-debug.apk
"""
    result = _filter_screenshot_paths(ls_output)
    assert result == [
        "gs://bucket/run/MediumPhone.arm-34-en-portrait/artifacts/0.png",
        "gs://bucket/run/MediumPhone.arm-34-en-portrait/artifacts/1.png",
    ]


def test_filter_screenshot_paths_respects_limit():
    ls_output = "\n".join(f"gs://bucket/artifacts/{i}.png" for i in range(10))
    assert len(_filter_screenshot_paths(ls_output, limit=3)) == 3


def test_filter_screenshot_paths_empty_when_none_match():
    assert _filter_screenshot_paths("gs://bucket/video.mp4\ngs://bucket/logcat") == []


def test_extract_json_object_from_json_code_fence():
    # design_qa_check에서 실제로 재현된 응답 형태: ```json 펜스로 감싸서 답함.
    text = '```json\n{"verdict": "mismatch", "detail": "배경색이 스펙과 다릅니다."}\n```'
    result = _extract_json_object(text)
    assert result == {"verdict": "mismatch", "detail": "배경색이 스펙과 다릅니다."}


def test_extract_json_object_from_plain_json():
    result = _extract_json_object('{"verdict": "match", "detail": "일치함"}')
    assert result == {"verdict": "match", "detail": "일치함"}


def test_extract_json_object_ignores_trailing_text_after_fence():
    # 코드펜스 우선 추출이면, 펜스 뒤에 다른 중괄호가 더 있어도 안 섞여야 한다
    # — first-'{'~last-'}' 방식으로 자르면 "Extra data" 파싱 에러가 나던 버그.
    text = '```json\n{"verdict": "match", "detail": "ok"}\n```\n참고로 다른 예시는 {"foo": "bar"} 같은 형태입니다.'
    result = _extract_json_object(text)
    assert result == {"verdict": "match", "detail": "ok"}


def test_design_mismatch_flips_passed_to_false_and_needs_rework():
    """핵심 회귀 테스트: 기능은 통과해도 디자인이 스펙과 다르면 실제로
    needs_rework가 켜져서 자동 재작업 루프를 타야 한다 — 예전엔 메시지만
    남기고 그대로 통과 처리돼서 디자인 불일치가 방치됐었다."""
    result = {"passed": True, "summary": "Passed"}
    outputs = _finalize_qa_outputs(result, video_ok=True, manual_count=0,
                                    design_mismatch_feedback="배경색이 스펙과 다릅니다.")
    assert outputs["passed"] is False
    assert outputs["needs_rework"] is True
    assert "배경색이 스펙과 다릅니다." in outputs["feedback"]


def test_no_design_mismatch_keeps_original_functional_result():
    result = {"passed": True, "summary": "Passed"}
    outputs = _finalize_qa_outputs(result, video_ok=True, manual_count=0,
                                    design_mismatch_feedback=None)
    assert outputs["passed"] is True
    assert "needs_rework" not in outputs


def test_functional_failure_preserved_even_without_design_check():
    result = {"passed": False, "summary": "Failed"}
    outputs = _finalize_qa_outputs(result, video_ok=False, manual_count=0,
                                    design_mismatch_feedback=None)
    assert outputs["passed"] is False
    assert "needs_rework" not in outputs  # 기존 동작 그대로 유지


def test_resolve_target_branch_prefers_implement_over_stale_autotest():
    """핵심 회귀 테스트: QA가 몇 주 된 낡은 브랜치를 계속 테스트하던 실제 사고.
    autotest의 예전 completed 결과가 context에 같이 실려도, 방금 implement가
    만든 새 브랜치가 이겨야 한다."""
    context = {
        "implement": {"agent": "implement", "branch": "ai-implement/new-branch", "pr_number": 11},
        "autotest": {"agent": "autotest", "branch": "ai-implement/ancient-stale-branch", "pr_number": 6},
    }
    assert _resolve_target_branch(context) == "ai-implement/new-branch"


def test_resolve_target_branch_order_independent():
    # dict 순서가 바뀌어도(예: autotest가 먼저 와도) implement가 항상 이겨야 한다.
    context = {
        "autotest": {"branch": "stale"},
        "implement": {"branch": "fresh"},
    }
    assert _resolve_target_branch(context) == "fresh"


def test_resolve_target_branch_falls_back_when_no_implement():
    context = {"autotest": {"branch": "only-option"}}
    assert _resolve_target_branch(context) == "only-option"


def test_resolve_target_branch_none_when_nothing_available():
    assert _resolve_target_branch({}) is None
    assert _resolve_target_branch({"planning": {"summary": "x"}}) is None


# ── _extract_scenario_test_code ─────────────────────────────────────
# 회귀 테스트 — 시나리오 테스트 생성 응답이 max_tokens에서 잘리면 닫는 ```가
# 없어서 코드 추출이 실패하는데, 예전엔 그때 원문(마커 포함)을 그대로 .dart
# 파일에 써서 컴파일 에러를 "시나리오 실패"로 잘못 보고했다. 이 버그 하나가
# counter-app에서 QA 재작업 예산(3/3)을 통째로 날려서 구현이 이미 맞았는데도
# 파이프라인이 "수동 확인 필요"로 멈췄다. 완전한 코드 블록이 없으면 무조건
# (None, 이유)로 건너뛰어야 한다.

def test_extracts_code_from_well_formed_fence():
    text = "여기 코드입니다:\n```dart\nvoid main() {}\n```\n끝."
    code, skip_reason = _extract_scenario_test_code(text, "end_turn")
    assert code == "void main() {}"
    assert skip_reason is None


def test_truncated_response_skips_without_using_raw_text():
    # 닫는 ```가 없는, 실제로 잘린 응답을 흉내낸다.
    text = "```dart\nimport 'package:flutter/material.dart';\nvoid main() {"
    code, skip_reason = _extract_scenario_test_code(text, "max_tokens")
    assert code is None
    assert skip_reason is not None
    assert "잘렸습니다" in skip_reason


def test_missing_fence_skips_instead_of_writing_raw_text_as_code():
    # stop_reason은 정상(end_turn)인데 모델이 형식을 안 지켜 펜스가 아예 없는 경우.
    text = "죄송합니다, 코드를 생성할 수 없습니다."
    code, skip_reason = _extract_scenario_test_code(text, "end_turn")
    assert code is None
    assert "코드 블록" in skip_reason


def test_empty_fence_skips_with_reason():
    text = "```dart\n\n```"
    code, skip_reason = _extract_scenario_test_code(text, "end_turn")
    assert code is None
    assert skip_reason == "LLM이 테스트 코드를 생성하지 않음"


# ── _list_source_files ───────────────────────────────────────────────
# 회귀 테스트 — 파일당 한도(2000자, 나중엔 6000자)가 있던 시절, 실제 화면
# 파일(counter_screen.dart, 9KB)의 버튼 위젯 코드가 파일 뒷부분에 있어서
# 통째로 잘려나갔다. 그 결과 QA 시나리오 생성 LLM이 실제로는
# FloatingActionButton을 쓰는 코드를 보고도 "ElevatedButton"으로 지어내 매
# 라운드 다른 위젯 타입을 기대하는 테스트를 써서 실패가 반복됐다
# (counter-app에서 실제로 재현). 이제 파일 단위 절단을 아예 없애고 전체
# 합계에만 훨씬 넉넉한 안전장치를 둔다.

def test_realistic_9kb_screen_file_included_in_full_with_no_per_file_cut(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    # 앞부분은 평범한 위젯, 실제 버튼 위젯은 뒷부분(옛 2000/6000자 한도 밖)에 있다.
    filler = "// filler\n" * 700  # 약 7000자 — 예전 6000자 한도도 넘김
    content = filler + "\nFloatingActionButton(\n  onPressed: _increment,\n),\n"
    assert len(content) > 7000
    (lib / "counter_screen.dart").write_text(content)

    result = _list_source_files(str(tmp_path))
    assert "FloatingActionButton" in result
    assert "이하 생략" not in result


def test_single_small_file_never_marked_truncated(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "main.dart").write_text("void main() {}\n")

    result = _list_source_files(str(tmp_path))
    assert "이하 생략" not in result


def test_total_beyond_safety_limit_is_marked_truncated(tmp_path):
    # 파일 하나가 아니라 "전체 합계"가 안전 한도를 넘는, 비정상적인 경우만
    # 잘려야 한다 — 이 한도는 실제 손으로 짠 Flutter 화면 코드로는 절대 안
    # 걸릴 만큼 넉넉해야 하므로(그래야 또 같은 사고가 안 난다), 실제로
    # 넘겼을 때만 마커가 붙는지 확인한다.
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "huge.dart").write_text("x" * (SOURCE_TOTAL_EXCERPT_LIMIT + 500))

    result = _list_source_files(str(tmp_path))
    assert "이하 생략" in result


def test_missing_lib_dir_reports_clearly():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        result = _list_source_files(d)
        assert "lib/ 디렉토리 자체가 없음" in result


# ── determine_build_command ─────────────────────────────────────────
# 회귀 테스트 — 예전 child-care-medication 프로젝트의 관례(android/app/
# build.gradle.kts에 "qa" flavor + lib/main_test.dart 진입점)를 다른 프로젝트에
# 강제하면 안 된다. 새 프로젝트를 만들 때 이 부분을 따로 설정할 필요가 없다는
# 게 보장돼야 한다 — 있으면 쓰고, 없으면(대부분의 새 프로젝트) 평범한 디버그
# 빌드로 자연스럽게 대체돼야 한다.

def test_no_pubspec_means_not_a_flutter_project_yet(tmp_path):
    cmd, reason = determine_build_command(str(tmp_path))
    assert cmd is None
    assert "pubspec.yaml" in reason


def test_project_without_android_dir_falls_back_to_plain_debug_build(tmp_path):
    # 새로 만든 프로젝트는 보통 android/ 폴더가 아직 커스터마이즈 안 된 상태다.
    (tmp_path / "pubspec.yaml").write_text("name: some_new_app\n")
    cmd, reason = determine_build_command(str(tmp_path))
    assert reason is None
    assert cmd == ["flutter", "build", "apk", "--debug"]


def test_project_with_gradle_but_no_qa_flavor_falls_back_to_plain_debug_build(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: some_new_app\n")
    android_app = tmp_path / "android" / "app"
    android_app.mkdir(parents=True)
    (android_app / "build.gradle.kts").write_text('android { defaultConfig { } }\n')
    cmd, reason = determine_build_command(str(tmp_path))
    assert reason is None
    assert cmd == ["flutter", "build", "apk", "--debug"]


def test_qa_flavor_without_test_entry_point_still_falls_back(tmp_path):
    # flavor는 있지만 main_test.dart 진입점이 없으면(child-care-medication
    # 컨벤션의 절반만 있는 경우) 억지로 flavor 빌드를 시도하지 않는다.
    (tmp_path / "pubspec.yaml").write_text("name: some_new_app\n")
    android_app = tmp_path / "android" / "app"
    android_app.mkdir(parents=True)
    (android_app / "build.gradle.kts").write_text('productFlavors { create("qa") {} }\n')
    cmd, reason = determine_build_command(str(tmp_path))
    assert reason is None
    assert cmd == ["flutter", "build", "apk", "--debug"]


def test_qa_flavor_convention_still_used_when_both_present(tmp_path):
    # child-care-medication류 컨벤션이 실제로 있는 프로젝트에서는 여전히 써야 한다
    # (레거시 지원 유지 — 새 프로젝트에 강제하지 않는 것과 별개).
    (tmp_path / "pubspec.yaml").write_text("name: legacy_app\n")
    android_app = tmp_path / "android" / "app"
    android_app.mkdir(parents=True)
    (android_app / "build.gradle.kts").write_text('productFlavors { create("qa") {} }\n')
    (tmp_path / "lib" / "main_test.dart").parent.mkdir(exist_ok=True)
    (tmp_path / "lib" / "main_test.dart").write_text("void main() {}\n")
    cmd, reason = determine_build_command(str(tmp_path))
    assert reason is None
    assert cmd == ["flutter", "build", "apk", "--flavor", "qa", "--debug", "-t", "lib/main_test.dart"]


def test_scenario_test_cmd_wraps_flutter_test_in_xvfb():
    # integration_test 기반 시나리오 테스트는 flutter-tester가 아니라 실제 Linux
    # 데스크톱 앱(GTK 창)으로 빌드돼서, 디스플레이 없는 컨테이너에서 xvfb 없이
    # 돌리면 빌드는 성공해놓고 실행만 "Error waiting for a debug connection"으로
    # 항상 실패했다(recoveryfit에서 실제 재현 — 하루 종일 모든 QA 라운드가 이걸로
    # 실패). xvfb-run으로 감싸야 실제로 끝까지 실행된다.
    assert _scenario_test_cmd("integration_test/scenario_test.dart") == [
        "xvfb-run", "-a", "flutter", "test", "integration_test/scenario_test.dart",
    ]
