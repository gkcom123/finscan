"""Period continuity. The bug this catches is invisible to every other check:
the numbers are right, they are just in the wrong column."""
from __future__ import annotations

from datetime import date

from finscan.periods import check_continuity, infer_cadence_days, parse_date

QUARTERS = {30: "2024-09-30", 31: "2024-12-31", 32: "2025-03-31", 33: "2025-06-30"}
YEARS = {3: "2022-12-31", 4: "2023-12-31", 5: "2024-12-31"}


def test_parse_date_handles_the_formats_models_use():
    assert parse_date("2025-06-30") == date(2025, 6, 30)
    assert parse_date("30.06.2025") == date(2025, 6, 30)
    assert parse_date(date(2025, 6, 30)) == date(2025, 6, 30)
    assert parse_date("Particulars") is None
    assert parse_date(None) is None


def test_cadence_is_inferred_from_the_model_not_assumed():
    assert 85 <= infer_cadence_days([parse_date(d) for d in QUARTERS.values()]) <= 95
    assert 360 <= infer_cadence_days([parse_date(d) for d in YEARS.values()]) <= 370


def test_the_next_quarter_passes():
    r = check_continuity(QUARTERS, 33, "2025-09-30", "quarter")
    assert r.ok and r.code == "period_continuous"


def test_a_gap_blocks_the_write():
    """The real case: a model last updated to Jun-2025, fed a Q1-2026 filing."""
    r = check_continuity(QUARTERS, 33, "2026-03-31", "quarter")
    assert not r.ok
    assert r.code == "period_gap"
    assert r.expected_date == date(2025, 9, 30)
    assert round(r.periods_off) == 2
    assert "Sep-2025 slot" in r.message


def test_re_uploading_a_period_already_in_the_model_is_blocked():
    r = check_continuity(QUARTERS, 33, "2025-06-30", "quarter")
    assert not r.ok and r.code == "period_already_present"


def test_month_end_drift_is_tolerated():
    """A 4-4-5 calendar closing 28-Sep is still the next quarter."""
    r = check_continuity(QUARTERS, 33, "2025-09-28", "quarter")
    assert r.ok


def test_annual_models_use_an_annual_cadence():
    assert check_continuity(YEARS, 5, "2025-12-31", "year").ok
    assert not check_continuity(YEARS, 5, "2027-12-31", "year").ok


def test_missing_information_degrades_to_a_pass_with_a_note():
    assert check_continuity({}, 3, "2025-09-30").ok is True
    assert check_continuity(QUARTERS, 33, None).code == "no_reported_date"


def test_graph_blocks_the_write_on_a_period_gap(workspace, monkeypatch, confirmed):
    """End to end: nothing may be written when the period does not line up."""
    from conftest import sample_extraction
    from finscan.graph.build import run as run_graph
    from finscan.schemas import Extraction

    far_future = sample_extraction()
    far_future.meta.period_end_date = "2030-06-30"
    far_future.meta.period_label = "Q1 FY2031"

    class _Stub:
        def __init__(self, schema):
            self.schema = schema

        def invoke(self, _m):
            return far_future if self.schema is Extraction else self.schema(matches=[])

    monkeypatch.setattr("finscan.extract.extractor.structured", lambda s: _Stub(s))
    monkeypatch.setattr("finscan.llm.factory.structured", lambda s: _Stub(s))

    run_graph(str(workspace["pdf"]), str(workspace["dated"]), company="dated",
              use_llm_mapping=False, dry_run=True)
    confirmed("dated", sheets=["P&L Summary"])

    state = run_graph(str(workspace["pdf"]), str(workspace["dated"]), company="dated",
                      output_path=str(workspace["dir"] / "dated_out.xlsx"),
                      use_llm_mapping=False)

    assert state["status"] == "needs_review"
    assert any(i.code == "period_gap" for i in state["issues"])
    assert state.get("write_result") is None
    assert not (workspace["dir"] / "dated_out.xlsx").exists()
