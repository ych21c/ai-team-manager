"""
회귀 테스트 — stage_completed의 summary가 길이 제한에 걸려 잘려서 Designer의
상세 스펙(AppBar/버튼/텍스트 스타일 등)이 통째로 사라지던 사고.
counter-app에서 실제로 재현됨: QA/다음 스테이지가 "background": "#FFFFFF" 근처
에서 잘린 요약만 보고 그게 스펙 전체인 줄 알았다. 처음엔 400자→6000자로 상한을
올려서 고쳤는데, 화면이 많은 프로젝트(recoveryFit)에서 6000자도 다시 잘리는
사고가 재발했다 — 그래서 상한 숫자를 조정하는 대신 아예 안 자르게 바꿨다.
build_summary는 이제 항상 full_response를 그대로 반환한다.

실행: cd agents/base && pip install pytest && pytest test_agent.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ANTHROPIC_API_KEY", "")

from agent import (
    build_summary,
    extract_design_map,
    parse_scenario_mockups,
    parse_triage_decision,
    _prior_scenario_mockups,
)


def test_short_response_not_truncated():
    text = "짧은 응답입니다."
    assert build_summary(text, "designer") == text


def test_very_long_response_never_truncated():
    # counter-app/recoveryFit에서 실제로 잘렸던 것과 비슷하거나 훨씬 긴 응답도
    # 이제 어떤 길이든 그대로 보존돼야 한다 — 더 이상 상한 자체가 없음.
    long_text = "x" * 50000
    result = build_summary(long_text, "designer")
    assert result == long_text
    assert "이하 생략" not in result


def test_response_with_json_spec_survives_intact():
    long_json_spec = '{"background": "#FFFFFF"' + ", " + '"appbar": "purple"' * 100
    assert len(long_json_spec) > 400
    result = build_summary(long_json_spec, "designer")
    assert result == long_json_spec
    assert "이하 생략" not in result
    assert "이하 생략" not in result


# ── parse_scenario_mockups ─────────────────────────────────────────
# 회귀 테스트 — design_preview.html 한 파일을 계속 덮어써서 재요청하면 이전
# 시나리오 목업이 사라지던 문제. 이제 시나리오(Jira 스토리)별로 나눠 저장한다.

def test_parses_multiple_tagged_scenarios():
    response = (
        "## SCENARIO:ATM-2\n```html\n<html>login</html>\n```\n"
        "## SCENARIO:ATM-3\n```html\n<html>home</html>\n```\n"
    )
    result = parse_scenario_mockups(response)
    assert result == {"ATM-2": "<html>login</html>", "ATM-3": "<html>home</html>"}


def test_falls_back_to_main_when_no_scenario_marker():
    response = "```html\n<html>legacy</html>\n```"
    result = parse_scenario_mockups(response)
    assert result == {"main": "<html>legacy</html>"}


def test_no_html_block_returns_empty():
    assert parse_scenario_mockups("그냥 텍스트만 있는 응답입니다") == {}


def test_rejects_scenario_key_with_path_traversal():
    response = "## SCENARIO:../../etc\n```html\n<html>evil</html>\n```\n"
    assert parse_scenario_mockups(response) == {}


# ── extract_design_map / _prior_scenario_mockups ──────────────────────
# 회귀 대비 — recoveryFit(화면 12개)처럼 화면이 많은 프로젝트에서 화면 1개
# 재작업 피드백을 보내도 designer_output.md 전체(다른 11개 화면 HTML 포함)가
# 매번 프롬프트에 통째로 다시 들어가던 문제. "맵"(화면 목록/디자인 시스템 —
# 시나리오 HTML 앞부분)만 따로 저장해두고, 재작업 대상 화면의 기존 목업만
# 골라 넣도록 분리했다.

def test_extract_design_map_returns_preamble_before_first_scenario():
    response = (
        '{"screens": ["login", "home"], "design_tokens": {"primary": "#000"}}\n\n'
        "## SCENARIO:ATM-5\n```html\n<html>login</html>\n```\n"
        "## SCENARIO:ATM-6\n```html\n<html>home</html>\n```\n"
    )
    result = extract_design_map(response)
    assert "design_tokens" in result
    assert "login</html>" not in result
    assert "home</html>" not in result


def test_extract_design_map_empty_when_no_scenario_marker():
    assert extract_design_map("그냥 텍스트만 있는 응답입니다") == ""


def test_prior_scenario_mockups_prefers_applied_over_pending(tmp_path):
    applied_dir = tmp_path / "design" / "applied"
    pending_dir = tmp_path / "design" / "pending"
    applied_dir.mkdir(parents=True)
    pending_dir.mkdir(parents=True)
    (applied_dir / "ATM-5.html").write_text("<button>최신 버전</button>")
    (pending_dir / "ATM-5.html").write_text("<button>옛 버전</button>")

    result = _prior_scenario_mockups(str(tmp_path), ["ATM-5"])

    assert "최신 버전" in result
    assert "옛 버전" not in result


def test_prior_scenario_mockups_only_includes_target_keys(tmp_path):
    # recoveryFit 사고 재현 방지 — 화면 8개 중 1개만 대상이면 나머지 7개는
    # 파일이 있어도 프롬프트에 안 들어가야 한다.
    applied_dir = tmp_path / "design" / "applied"
    applied_dir.mkdir(parents=True)
    for key in [f"ATM-{n}" for n in range(5, 13)]:
        (applied_dir / f"{key}.html").write_text(f"<html>{key}</html>")

    result = _prior_scenario_mockups(str(tmp_path), ["ATM-5"])

    assert "ATM-5" in result
    for key in [f"ATM-{n}" for n in range(6, 13)]:
        assert key not in result


def test_prior_scenario_mockups_skips_unsafe_keys(tmp_path):
    result = _prior_scenario_mockups(str(tmp_path), ["../../etc"])
    assert result == ""


def test_prior_scenario_mockups_empty_when_no_files(tmp_path):
    assert _prior_scenario_mockups(str(tmp_path), ["ATM-5"]) == ""


# ── parse_triage_decision ────────────────────────────────────────────
# 회귀 대비 — 채팅 트리아지(PM이 후속 요청을 design/implement/none 중 어디로
# 돌릴지 판단)는 stage_completed를 무조건 내보내야 orchestrator가 멈추지
# 않는다. LLM 응답이 깨져도 예외 없이 안전한 기본값(scope=none)으로
# 폴백해야 한다.

def test_parses_valid_json_decision():
    text = '{"scope": "design", "feedback": "버튼이 사라짐", "reply": "디자인부터 다시 만들게요"}'
    result = parse_triage_decision(text)
    assert result == {
        "scope": "design", "target": None, "new_story_title": "",
        "feedback": "버튼이 사라짐", "reply": "디자인부터 다시 만들게요",
    }


def test_extracts_json_wrapped_in_prose_and_code_fence():
    text = (
        "판단 결과는 다음과 같습니다:\n```json\n"
        '{"scope": "implement", "feedback": "로그인 버튼 클릭 시 크래시 수정", "reply": "구현만 다시 확인할게요"}'
        "\n```\n이상입니다."
    )
    result = parse_triage_decision(text)
    assert result["scope"] == "implement"
    assert "크래시" in result["feedback"]


def test_invalid_scope_falls_back_to_none():
    text = '{"scope": "release", "feedback": "x", "reply": "y"}'
    result = parse_triage_decision(text)
    assert result["scope"] == "none"


def test_garbage_text_falls_back_to_none_with_reply_never_raises():
    result = parse_triage_decision("이건 그냥 잡담이에요, JSON이 전혀 아닙니다")
    assert result["scope"] == "none"
    assert result["reply"]  # 빈 응답을 사용자에게 그냥 안 보여주면 안 됨


def test_empty_response_falls_back_to_none_with_default_reply():
    result = parse_triage_decision("")
    assert result["scope"] == "none"
    assert result["reply"] == "요청을 이해하지 못했습니다."


# ── parse_triage_decision: target/new_story_title ────────────────────────
# 회귀 대비 — "1번 화면 로고 이상해서 다시 디자인해줘" 같은 요청이 이슈 8개
# 전체가 아니라 그 이슈 하나로만 재작업 범위가 좁혀지려면, PM 트리아지가 어떤
# 기존 이슈를 가리키는지(target=key) 혹은 완전히 새 기능인지(target="new")를
# 같이 판단해서 넘겨야 한다. 이 파싱 단계는 project_jira 상태를 모르므로 실제
# 키 존재 여부는 검증하지 않는다(orchestrator._handle_chat_triage_result가 함) —
# 여기선 형식만 방어적으로 파싱되는지만 본다.

def test_target_existing_key_is_parsed_through():
    text = '{"scope": "design", "target": "ATM-5", "new_story_title": "", "feedback": "로고가 이상함", "reply": "ATM-5만 다시 만들게요"}'
    result = parse_triage_decision(text)
    assert result["target"] == "ATM-5"
    assert result["new_story_title"] == ""


def test_target_new_carries_story_title():
    text = '{"scope": "design", "target": "new", "new_story_title": "다크모드 지원", "feedback": "다크모드 추가", "reply": "새 이슈로 등록할게요"}'
    result = parse_triage_decision(text)
    assert result["target"] == "new"
    assert result["new_story_title"] == "다크모드 지원"


def test_missing_target_defaults_to_none():
    text = '{"scope": "implement", "feedback": "크래시 수정", "reply": "구현만 다시 확인할게요"}'
    result = parse_triage_decision(text)
    assert result["target"] is None
    assert result["new_story_title"] == ""


def test_garbage_target_value_falls_back_to_none_string_not_raises():
    # target이 문자열이 아닌 이상한 타입(숫자 등)이어도 예외 없이 안전하게 처리돼야 함
    text = '{"scope": "design", "target": 123, "feedback": "x", "reply": "y"}'
    result = parse_triage_decision(text)
    assert result["target"] == "123"
