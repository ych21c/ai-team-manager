"""
mockup_guidance() 회귀 테스트.

scenario_keys가 있으면(이슈 하나 이상으로 범위를 좁힌 재작업) OpenHands에게 그 화면만
반영하고 다른 화면은 건드리지 말라고 안내해야 하고, 없으면(최초 구현/전체
재작업) 예전처럼 모든 화면을 다 확인하라고 안내해야 한다. 이 문자열 하나로
"디자인을 이슈 하나만 재작업했는데 코드는 전체가 다시 만들어지는" 문제를
막으려는 거라, 정확한 문구가 조건에 따라 갈리는지가 핵심이다.

실행: cd agents/implement_openhands && pytest tests/ -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prompt_helpers import mockup_guidance


def test_no_scenario_key_asks_to_check_every_screen():
    guidance = mockup_guidance(None)
    assert "전부 확인" in guidance
    assert "design/applied/" not in guidance  # 특정 파일을 콕 집어 말하지 않음


def test_scenario_key_narrows_to_that_screen_only():
    guidance = mockup_guidance(["ATM-5"])
    assert "design/applied/ATM-5.html" in guidance
    assert "다른 화면" in guidance and "건드리지 마세요" in guidance
    assert "전부 확인" not in guidance


def test_multiple_scenario_keys_narrows_to_those_screens_only():
    guidance = mockup_guidance(["ATM-5", "ATM-10"])
    assert "design/applied/ATM-5.html" in guidance
    assert "design/applied/ATM-10.html" in guidance
    assert "다른 화면" in guidance and "건드리지 마세요" in guidance
    assert "전부 확인" not in guidance
