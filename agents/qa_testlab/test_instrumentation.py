"""
회귀 테스트 — 시나리오 테스트를 로컬 시뮬레이션(flutter test)에서 그치지 않고
실제 기기(Firebase Test Lab instrumentation)에서도 확인하도록 추가한 부분.

1. ensure_integration_test_dependency: SCENARIO_TEST_FILE이 integration_test
   패키지를 쓰는데, `flutter create` 기본 pubspec.yaml엔 이게 없어서 결정적으로
   추가해줘야 한다 — pubspec.yaml 편집 로직만 실제 flutter 없이 검증한다.
2. build_instrumentation_apks: 실기기 확인용 앱/테스트 APK 두 개를 빌드하는
   로직 — subprocess(run)를 가짜로 대체해서 성공/실패 분기와 산출 경로가
   맞는지 검증한다.

실행: cd agents/qa_testlab && pytest test_instrumentation.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run as run_module
from run import build_instrumentation_apks, ensure_integration_test_dependency


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── ensure_integration_test_dependency ──────────────────────────────────────

def test_missing_pubspec_is_noop(tmp_path):
    assert ensure_integration_test_dependency(str(tmp_path)) is False


def test_adds_integration_test_under_existing_dev_dependencies(tmp_path):
    pubspec = tmp_path / "pubspec.yaml"
    pubspec.write_text(
        "name: dummy\n"
        "dependencies:\n"
        "  flutter:\n"
        "    sdk: flutter\n"
        "dev_dependencies:\n"
        "  flutter_test:\n"
        "    sdk: flutter\n"
    )

    changed = ensure_integration_test_dependency(str(tmp_path))

    assert changed is True
    content = pubspec.read_text()
    assert "  integration_test:\n    sdk: flutter" in content
    # 기존 dev_dependencies 항목(flutter_test)은 그대로 보존돼야 한다.
    assert "flutter_test:" in content
    # 파일 전체가 여전히 파싱 가능한 형태인지(들여쓰기 레벨이 깨지지 않았는지)
    lines = content.splitlines()
    idx = lines.index("dev_dependencies:")
    assert lines[idx + 1] == "  integration_test:"
    assert lines[idx + 2] == "    sdk: flutter"


def test_creates_dev_dependencies_section_when_missing(tmp_path):
    pubspec = tmp_path / "pubspec.yaml"
    pubspec.write_text("name: dummy\ndependencies:\n  flutter:\n    sdk: flutter\n")

    changed = ensure_integration_test_dependency(str(tmp_path))

    assert changed is True
    content = pubspec.read_text()
    assert "dev_dependencies:" in content
    assert "  integration_test:\n    sdk: flutter" in content


def test_noop_when_already_present(tmp_path):
    pubspec = tmp_path / "pubspec.yaml"
    original = (
        "name: dummy\n"
        "dev_dependencies:\n"
        "  integration_test:\n"
        "    sdk: flutter\n"
    )
    pubspec.write_text(original)

    changed = ensure_integration_test_dependency(str(tmp_path))

    assert changed is False
    assert pubspec.read_text() == original


# ── build_instrumentation_apks ───────────────────────────────────────────────

def test_no_android_dir_fails_without_running_any_command(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(run_module, "run", lambda *a, **k: calls.append(a) or _FakeCompletedProcess())

    ok, detail, app_apk, test_apk = build_instrumentation_apks(str(tmp_path))

    assert ok is False
    assert "android" in detail
    assert app_apk is None and test_apk is None
    assert calls == []


def test_android_test_apk_build_failure_reported(tmp_path, monkeypatch):
    (tmp_path / "android").mkdir()

    def fake_run(cmd, cwd=None, timeout=60):
        if cmd == ["./gradlew", "app:assembleAndroidTest"]:
            return _FakeCompletedProcess(returncode=1, stderr="androidTest config error")
        raise AssertionError(f"예상 못한 명령: {cmd}")

    monkeypatch.setattr(run_module, "run", fake_run)

    ok, detail, app_apk, test_apk = build_instrumentation_apks(str(tmp_path))

    assert ok is False
    assert "androidTest APK 빌드 실패" in detail
    assert app_apk is None and test_apk is None


def test_target_apk_build_failure_reported(tmp_path, monkeypatch):
    (tmp_path / "android").mkdir()

    def fake_run(cmd, cwd=None, timeout=60):
        if cmd == ["./gradlew", "app:assembleAndroidTest"]:
            return _FakeCompletedProcess(returncode=0)
        if cmd[:2] == ["./gradlew", "app:assembleDebug"]:
            return _FakeCompletedProcess(returncode=1, stderr="compile error in scenario_test.dart")
        raise AssertionError(f"예상 못한 명령: {cmd}")

    monkeypatch.setattr(run_module, "run", fake_run)

    ok, detail, app_apk, test_apk = build_instrumentation_apks(str(tmp_path))

    assert ok is False
    assert "시나리오 진입점 앱 APK 빌드 실패" in detail
    assert app_apk is None and test_apk is None


def test_success_returns_both_apk_paths(tmp_path, monkeypatch):
    workspace = str(tmp_path)
    (tmp_path / "android").mkdir()
    app_dir = tmp_path / "build" / "app" / "outputs" / "apk" / "debug"
    test_dir = tmp_path / "build" / "app" / "outputs" / "apk" / "androidTest" / "debug"
    app_dir.mkdir(parents=True)
    test_dir.mkdir(parents=True)
    (app_dir / "app-debug.apk").write_bytes(b"app")
    (test_dir / "app-debug-androidTest.apk").write_bytes(b"test")

    def fake_run(cmd, cwd=None, timeout=60):
        if cmd == ["./gradlew", "app:assembleAndroidTest"]:
            return _FakeCompletedProcess(returncode=0)
        if cmd[:2] == ["./gradlew", "app:assembleDebug"]:
            assert cmd[2] == f"-Ptarget={workspace}/integration_test/scenario_test.dart"
            return _FakeCompletedProcess(returncode=0)
        raise AssertionError(f"예상 못한 명령: {cmd}")

    monkeypatch.setattr(run_module, "run", fake_run)

    ok, detail, app_apk, test_apk = build_instrumentation_apks(workspace)

    assert ok is True
    assert detail == ""
    assert app_apk == str(app_dir / "app-debug.apk")
    assert test_apk == str(test_dir / "app-debug-androidTest.apk")
