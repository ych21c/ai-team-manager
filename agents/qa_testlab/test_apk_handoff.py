"""
회귀 테스트 — Implement(agents/implement_openhands/run.py)가 결정적으로 빌드해서
남긴 APK를 QA가 재빌드 없이 그대로 쓸지 판단하는 _should_reuse_implement_apk()의
순수 로직만 검증한다. QA가 처음부터 다시 빌드하면 (1) 빌드를 두 번 하고 (2)
"Implement가 실제로 만든 그 바이너리"가 아니라 QA가 새로 빌드한 별개의 바이너리를
테스트하게 되므로, 이 판단이 틀리면 재빌드 없이 넘어가선 안 될 상황(빌드 실패,
handoff 파일 없음, qa flavor 특수 빌드)에서도 잘못된 APK를 쓰게 된다.

실행: cd agents/qa_testlab && pytest test_apk_handoff.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run import _should_reuse_implement_apk

PLAIN_BUILD_CMD = ["flutter", "build", "apk", "--debug"]
QA_FLAVOR_BUILD_CMD = ["flutter", "build", "apk", "--flavor", "qa", "--debug", "-t", "lib/main_test.dart"]


def test_reuses_when_build_succeeded_and_file_exists():
    assert _should_reuse_implement_apk(PLAIN_BUILD_CMD, {"build_ok": True}, apk_exists=True) is True


def test_does_not_reuse_when_implement_build_failed():
    assert _should_reuse_implement_apk(PLAIN_BUILD_CMD, {"build_ok": False}, apk_exists=True) is False


def test_does_not_reuse_when_build_ok_missing_legacy_session():
    """이 기능 도입 전에 시작된 세션은 outputs에 build_ok 키 자체가 없다 —
    그런 경우는 실패로 취급해 안전하게 QA 자체 빌드로 폴백해야 한다."""
    assert _should_reuse_implement_apk(PLAIN_BUILD_CMD, {}, apk_exists=True) is False


def test_does_not_reuse_when_handoff_file_missing():
    assert _should_reuse_implement_apk(PLAIN_BUILD_CMD, {"build_ok": True}, apk_exists=False) is False


def test_does_not_reuse_for_qa_flavor_build():
    """qa flavor + main_test.dart 컨벤션을 쓰는 프로젝트는 Implement의 일반
    디버그 빌드로는 맞지 않으므로, Implement 빌드가 성공했어도 재사용하면 안 된다."""
    assert _should_reuse_implement_apk(QA_FLAVOR_BUILD_CMD, {"build_ok": True}, apk_exists=True) is False
