"""
회귀 테스트 — AAPT2/Gradle 데몬이 Docker VM 메모리 예산(7.75GiB)을 넘는
-Xmx8G 기본값 때문에 "Daemon startup failed"로 죽어서, 멀쩡한 구현이 needs_rework로
잘못 튕겨나가던 사고. counter-app 프로젝트에서 실제로 재현됐다 (BUILD FAILED in 1s).

실행: cd agents/qa_testlab && pip install pytest && pytest test_gradle_memory.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run import _cap_gradle_memory


def test_creates_gradle_properties_with_capped_heap_and_no_daemon(tmp_path):
    android_dir = tmp_path / "android"
    android_dir.mkdir()

    _cap_gradle_memory(str(tmp_path))

    props = (android_dir / "gradle.properties").read_text()
    assert "org.gradle.daemon=false" in props
    assert "org.gradle.jvmargs=-Xmx1536m" in props


def test_overrides_conflicting_existing_values_without_duplicating(tmp_path):
    android_dir = tmp_path / "android"
    android_dir.mkdir()
    (android_dir / "gradle.properties").write_text(
        "org.gradle.jvmargs=-Xmx8G\n"
        "android.useAndroidX=true\n"
    )

    _cap_gradle_memory(str(tmp_path))

    props = (android_dir / "gradle.properties").read_text()
    assert props.count("org.gradle.jvmargs=") == 1
    assert "org.gradle.jvmargs=-Xmx1536m" in props
    assert "-Xmx8G" not in props
    # 관련 없는 기존 설정은 그대로 보존돼야 한다.
    assert "android.useAndroidX=true" in props


def test_noop_when_no_android_directory(tmp_path):
    _cap_gradle_memory(str(tmp_path))
    assert not (tmp_path / "android").exists()
