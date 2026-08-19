"""
회귀 테스트 — 토큰/비용 누적을 프로젝트 전체 단일 합계가 아니라
스프린트 × 스테이지 단위로 나눠서 보는 기능. 사용자 요청: "비용은 스프린트
단위로 각 단계 누적해야함".

실행: cd orchestrator && pytest tests/test_token_totals.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


def _fresh():
    main.project_token_totals.clear()


def test_accumulates_within_same_sprint_and_stage_across_reruns():
    _fresh()
    main._accumulate_token_usage("p1", 1, "implement", {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01})
    main._accumulate_token_usage("p1", 1, "implement", {"input_tokens": 40, "output_tokens": 10, "cost_usd": 0.005})

    bucket = main.project_token_totals["p1"]["by_sprint"]["1"]["implement"]
    assert bucket == {"input_tokens": 140, "output_tokens": 60, "cost_usd": 0.015}


def test_different_stages_same_sprint_are_kept_separate():
    _fresh()
    main._accumulate_token_usage("p1", 1, "design", {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001})
    main._accumulate_token_usage("p1", 1, "qa", {"input_tokens": 20, "output_tokens": 15, "cost_usd": 0.002})

    stages = main.project_token_totals["p1"]["by_sprint"]["1"]
    assert stages["design"]["input_tokens"] == 10
    assert stages["qa"]["input_tokens"] == 20


def test_same_stage_across_different_sprints_are_kept_separate():
    _fresh()
    main._accumulate_token_usage("p1", 1, "implement", {"input_tokens": 100, "output_tokens": 0, "cost_usd": 0.0})
    main._accumulate_token_usage("p1", 2, "implement", {"input_tokens": 5, "output_tokens": 0, "cost_usd": 0.0})

    by_sprint = main.project_token_totals["p1"]["by_sprint"]
    assert by_sprint["1"]["implement"]["input_tokens"] == 100
    assert by_sprint["2"]["implement"]["input_tokens"] == 5


def test_design_stage_sums_both_agents_into_one_bucket():
    """design은 designer+architect 두 에이전트가 각자 stage_name="design"으로
    완료 보고한다 — 지금 동작(한 버킷에 합산)과 동일해야 한다."""
    _fresh()
    main._accumulate_token_usage("p1", 1, "design", {"input_tokens": 10, "output_tokens": 0, "cost_usd": 0.0})
    main._accumulate_token_usage("p1", 1, "design", {"input_tokens": 20, "output_tokens": 0, "cost_usd": 0.0})

    assert main.project_token_totals["p1"]["by_sprint"]["1"]["design"]["input_tokens"] == 30


def test_outputs_without_token_fields_are_skipped():
    _fresh()
    main._accumulate_token_usage("p1", 1, "autotest", {"passed": True})
    assert "p1" not in main.project_token_totals


def test_derive_lifetime_totals_sums_pre_migration_and_all_sprints():
    _fresh()
    main.project_token_totals["p1"] = {
        "pre_migration": {"input_tokens": 1000, "output_tokens": 500, "cost_usd": 0.5},
        "by_sprint": {
            "1": {"implement": {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01}},
            "2": {"qa": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001}},
        },
    }
    total = main._derive_lifetime_totals("p1")
    assert total["input_tokens"] == 1110
    assert total["output_tokens"] == 555
    assert round(total["cost_usd"], 3) == 0.511


def test_derive_lifetime_totals_for_unknown_project_is_zero():
    _fresh()
    assert main._derive_lifetime_totals("nope") == {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def test_migrate_wraps_old_flat_shape_into_pre_migration():
    old_flat = {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01}
    migrated = main._migrate_token_totals(old_flat)
    assert migrated == {"by_sprint": {}, "pre_migration": old_flat}


def test_migrate_is_idempotent_for_already_new_shape():
    new_shape = {"by_sprint": {"1": {"implement": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}}}, "pre_migration": None}
    assert main._migrate_token_totals(new_shape) == new_shape
