"""
build_and_handoff_apk() 회귀 테스트 — QA(agents/qa_testlab/run.py)가 같은 소스를
처음부터 다시 빌드하지 않도록, Implement가 결정적으로 빌드한 APK를 고정 경로에
남기는 로직. 실제 flutter/gradle 없이 subprocess 호출(run)만 가짜로 대체해서
성공/실패 각 분기와 handoff 파일 복사가 맞는지 검증한다.

실행: cd agents/implement_openhands && pytest tests/test_build_apk.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_apk
from build_apk import ARTIFACT_APK_NAME, build_and_handoff_apk


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _write_pubspec(workspace):
    os.makedirs(workspace, exist_ok=True)
    with open(os.path.join(workspace, "pubspec.yaml"), "w") as f:
        f.write("name: dummy\n")


def test_missing_pubspec_fails_without_running_any_command(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(build_apk, "run", lambda *a, **k: calls.append(a) or _FakeCompletedProcess())

    workspace = str(tmp_path / "ws")
    os.makedirs(workspace)

    ok, detail, path = build_and_handoff_apk(workspace, "proj1", artifacts_root=str(tmp_path))

    assert ok is False
    assert "pubspec.yaml" in detail
    assert path is None
    assert calls == []  # flutter 명령을 하나도 안 불러야 함


def test_pub_get_failure_reported(tmp_path, monkeypatch):
    workspace = str(tmp_path / "ws")
    _write_pubspec(workspace)

    def fake_run(cmd, cwd=None, timeout=60):
        if cmd[:2] == ["flutter", "pub"]:
            return _FakeCompletedProcess(returncode=1, stderr="network unreachable")
        raise AssertionError(f"예상 못한 명령 호출: {cmd}")

    monkeypatch.setattr(build_apk, "run", fake_run)

    ok, detail, path = build_and_handoff_apk(workspace, "proj1", artifacts_root=str(tmp_path))

    assert ok is False
    assert "pub get 실패" in detail
    assert path is None


def test_build_failure_reported(tmp_path, monkeypatch):
    workspace = str(tmp_path / "ws")
    _write_pubspec(workspace)

    def fake_run(cmd, cwd=None, timeout=60):
        if cmd[:2] == ["flutter", "pub"]:
            return _FakeCompletedProcess(returncode=0)
        if cmd[:2] == ["flutter", "build"]:
            return _FakeCompletedProcess(returncode=1, stderr="Gradle build failed")
        raise AssertionError(f"예상 못한 명령 호출: {cmd}")

    monkeypatch.setattr(build_apk, "run", fake_run)

    ok, detail, path = build_and_handoff_apk(workspace, "proj1", artifacts_root=str(tmp_path))

    assert ok is False
    assert "build apk 실패" in detail
    assert path is None


def test_missing_apk_after_successful_build_reported(tmp_path, monkeypatch):
    workspace = str(tmp_path / "ws")
    _write_pubspec(workspace)

    def fake_run(cmd, cwd=None, timeout=60):
        if cmd[:2] == ["flutter", "pub"]:
            return _FakeCompletedProcess(returncode=0)
        if cmd[:2] == ["flutter", "build"]:
            return _FakeCompletedProcess(returncode=0)
        if cmd[0] == "find":
            return _FakeCompletedProcess(returncode=0, stdout="")  # APK 못 찾음
        raise AssertionError(f"예상 못한 명령 호출: {cmd}")

    monkeypatch.setattr(build_apk, "run", fake_run)

    ok, detail, path = build_and_handoff_apk(workspace, "proj1", artifacts_root=str(tmp_path))

    assert ok is False
    assert "찾지 못함" in detail
    assert path is None


def test_success_copies_apk_to_fixed_handoff_path(tmp_path, monkeypatch):
    """핵심 케이스: 빌드가 성공하면 workspace(git 트리) 밖의
    {artifacts_root}/{project_id}-artifacts/app-debug.apk 로 바이트 그대로
    복사돼야 한다 — QA가 재빌드 없이 바로 쓸 파일이므로 경로/내용이 정확해야 한다."""
    workspace = str(tmp_path / "ws")
    _write_pubspec(workspace)

    built_apk_dir = os.path.join(workspace, "build", "app", "outputs", "flutter-apk")
    os.makedirs(built_apk_dir, exist_ok=True)
    built_apk_path = os.path.join(built_apk_dir, "app-debug.apk")
    with open(built_apk_path, "wb") as f:
        f.write(b"fake-apk-bytes")

    def fake_run(cmd, cwd=None, timeout=60):
        if cmd[:2] == ["flutter", "pub"]:
            return _FakeCompletedProcess(returncode=0)
        if cmd[:2] == ["flutter", "build"]:
            return _FakeCompletedProcess(returncode=0)
        if cmd[0] == "find":
            return _FakeCompletedProcess(returncode=0, stdout=built_apk_path + "\n")
        raise AssertionError(f"예상 못한 명령 호출: {cmd}")

    monkeypatch.setattr(build_apk, "run", fake_run)

    ok, detail, path = build_and_handoff_apk(workspace, "proj1", artifacts_root=str(tmp_path))

    assert ok is True
    assert detail == ""
    expected_path = os.path.join(str(tmp_path), "proj1-artifacts", ARTIFACT_APK_NAME)
    assert path == expected_path
    assert os.path.exists(expected_path)
    with open(expected_path, "rb") as f:
        assert f.read() == b"fake-apk-bytes"
    # 원본 workspace(git 트리) 밖에 있어야 한다 — QA가 별도 clone을 쓰는 이유와
    # 같은 이유로, git add -A 대상에 절대 섞이면 안 된다.
    assert not expected_path.startswith(workspace)


def test_gradle_memory_cap_applied_before_build(tmp_path, monkeypatch):
    """_cap_gradle_memory가 flutter build보다 먼저 호출돼서, 빌드 시점엔 이미
    gradle.properties가 힙 캡이 걸려 있어야 한다(다른 프로젝트와 동시에 떠도
    AAPT2가 죽지 않도록)."""
    workspace = str(tmp_path / "ws")
    _write_pubspec(workspace)
    os.makedirs(os.path.join(workspace, "android"))

    seen_jvmargs_at_build_time = []

    def fake_run(cmd, cwd=None, timeout=60):
        if cmd[:2] == ["flutter", "pub"]:
            return _FakeCompletedProcess(returncode=0)
        if cmd[:2] == ["flutter", "build"]:
            props = os.path.join(workspace, "android", "gradle.properties")
            with open(props) as f:
                seen_jvmargs_at_build_time.append(f.read())
            return _FakeCompletedProcess(returncode=1, stderr="stop here")  # 이후 단계 불필요
        raise AssertionError(f"예상 못한 명령 호출: {cmd}")

    monkeypatch.setattr(build_apk, "run", fake_run)

    build_and_handoff_apk(workspace, "proj1", artifacts_root=str(tmp_path))

    assert len(seen_jvmargs_at_build_time) == 1
    assert "org.gradle.jvmargs=-Xmx1536m" in seen_jvmargs_at_build_time[0]
    assert "org.gradle.daemon=false" in seen_jvmargs_at_build_time[0]
