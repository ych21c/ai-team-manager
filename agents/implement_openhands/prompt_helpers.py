"""
OpenHands에게 보내는 프롬프트 조각을 만드는 순수 로직만 모아둔 모듈.
run.py는 openhands.sdk(무거운 런타임 의존성)를 임포트하기 때문에 이 로직을
run.py 안에 그대로 두면 테스트가 SDK 설치를 요구하게 된다 — git_workspace.py와
같은 이유로 별도 모듈로 뽑아서 의존성 없이 테스트 가능하게 한다.
"""


def mockup_guidance(scenario_keys: list[str] | None) -> str:
    """design/applied/의 목업을 어디까지 반영해야 하는지 안내하는 문장.

    scenario_keys가 없으면(전체 재작업/최초 구현) 모든 화면을 다 확인하라고
    하고, scenario_keys가 있으면(하나 이상의 이슈로 범위를 좁힌 재작업) 그
    화면들만 반영하고 다른 화면은 건드리지 말라고 한다 — OpenHands는 자유도
    높은 코딩 에이전트라 이건 강제가 아니라 유도일 뿐이라는 점에 유의."""
    if scenario_keys:
        files = ", ".join(f"design/applied/{key}.html" for key in scenario_keys)
        return (
            f"{files}에 해당하는 화면만 반영하고, "
            f"다른 화면/기능은 이미 반영돼 있으니 절대 건드리지 마세요."
        )
    return "파일마다 다른 화면이니 전부 확인하고, 목업과 실제 앱 화면이 다르면 안 됩니다."
