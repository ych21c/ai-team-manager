"""
회귀 테스트 — Jira 이슈 계층을 Epic→Story→Subtask 3단으로 재구성하면서
_get_issue_types가 (에픽용, 스토리용, 서브태스크용) 3개 이름을 올바르게
골라내는지 확인한다.

배경: 이 프로젝트(ATM)는 원래 에픽 레벨(hierarchyLevel=1) 이슈 타입이 이슈
타입 스킴에 없어서, "작업"(Task, level 0)을 에픽 자리에, "하위 작업"
(Subtask, level -1)을 스토리 자리에 억지로 쓰고 있었다(2단 구성). 에픽
타입("에픽")을 스킴에 추가한 뒤에는 진짜 3단(에픽=1, 스토리=0, 서브태스크=-1)
계층을 쓸 수 있어야 하고, design/implement/qa 하위 작업은 그 밑의 서브태스크
레벨에 만든다. 이 선택 로직은 createmeta의 실제 응답 형태(orchestrator/
tests/test_jira_hierarchy_types.py 작성 시점에 실제 API로 확인한 값)를
그대로 fixture로 써서 검증한다.

실행: cd orchestrator && pytest tests/test_jira_hierarchy_types.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from atlassian_client import _pick_issue_types

# 실제 ATM 프로젝트의 createmeta 응답(에픽 타입 추가 후)에서 issuetypes만 발췌.
THREE_LEVEL_TYPES = [
    {"name": "작업", "subtask": False, "hierarchyLevel": 0},
    {"name": "하위 작업", "subtask": True, "hierarchyLevel": -1},
    {"name": "에픽", "subtask": False, "hierarchyLevel": 1},
]

# 에픽 타입을 스킴에 추가하기 전, 원래의 2단 구성.
TWO_LEVEL_TYPES = [
    {"name": "작업", "subtask": False, "hierarchyLevel": 0},
    {"name": "하위 작업", "subtask": True, "hierarchyLevel": -1},
]


def test_three_level_scheme_separates_epic_story_subtask():
    epic, story, child = _pick_issue_types(THREE_LEVEL_TYPES)
    assert epic == "에픽"
    assert story == "작업"
    assert child == "하위 작업"


def test_two_level_scheme_falls_back_epic_to_story_type():
    """에픽 레벨 타입이 스킴에 없으면(과거 이 프로젝트 상태), 에픽 자리도
    스토리용 타입으로 채워서 예전 2단 동작(Task=에픽, Subtask=스토리)을
    그대로 유지해야 한다 — 그래야 이 변경이 기존 프로젝트를 깨지 않는다."""
    epic, story, child = _pick_issue_types(TWO_LEVEL_TYPES)
    assert epic == "작업"
    assert story == "작업"
    assert child == "하위 작업"


def test_empty_types_falls_back_to_defaults():
    epic, story, child = _pick_issue_types([])
    assert epic == "Task"
    assert story == "Task"
    assert child == "Subtask"


def test_order_independent():
    """createmeta가 순서를 어떻게 주든(정렬 보장 없음) 같은 결과가 나와야 한다."""
    shuffled = list(reversed(THREE_LEVEL_TYPES))
    assert _pick_issue_types(shuffled) == _pick_issue_types(THREE_LEVEL_TYPES)
