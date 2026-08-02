"""Profiles are what make 100 companies a data problem rather than a code problem."""
from __future__ import annotations

from finscan.excel.discovery import discover
from finscan.profiles import CompanyProfile, ProfileStore, company_key


def test_company_key_normalises_suffixes_and_punctuation():
    assert company_key("Northwind Industries Ltd.") == "northwind_industries"
    assert company_key("northwind industries limited") == "northwind_industries"
    assert company_key("Acme Manufacturing Pvt Ltd") == "acme_manufacturing"
    assert company_key("") == "unknown"


def test_new_company_profile_starts_unconfirmed_with_scoped_sheets(workspace):
    plan = discover(workspace["acme"])
    pr = CompanyProfile.from_plan("acme_manufacturing", plan)

    assert pr.confirmed is False
    assert pr.enabled_sheets() == {"Consolidated P&L", "Standalone P&L"}
    assert "Segments" not in pr.enabled_sheets()


def test_store_round_trip_and_reuse(workspace):
    store = ProfileStore(workspace["profiles"])
    plan = discover(workspace["northwind"])

    pr, state = store.resolve("northwind_industries", plan)
    assert state == "new"
    store.save(pr.confirm(by="test"))

    again, state2 = store.resolve("northwind_industries", discover(workspace["northwind"]))
    assert state2 == "reused"
    assert again.confirmed is True
    assert again.confirmed_by == "test"


def test_drift_resets_confirmation_but_keeps_the_human_sheet_choices(workspace):
    from openpyxl import load_workbook

    store = ProfileStore(workspace["profiles"])
    pr, _ = store.resolve("acme_manufacturing", discover(workspace["acme"]))
    for s in pr.sheets:
        s.enabled = s.sheet == "Consolidated P&L"      # reviewer narrowed the scope
    store.save(pr.confirm(by="test"))

    wb = load_workbook(workspace["acme"])
    wb["Consolidated P&L"].insert_rows(9)
    wb.save(workspace["acme"])

    drifted, state = store.resolve("acme_manufacturing", discover(workspace["acme"]))
    assert state == "drifted"
    assert drifted.confirmed is False
    assert drifted.enabled_sheets() == {"Consolidated P&L"}, "the reviewer's choice survived"
    assert any("drift" in h for h in drifted.history)


def test_label_overrides_teach_the_agent_a_company_specific_caption(workspace, stub_llm):
    from finscan.graph.build import run as run_graph

    store = ProfileStore(workspace["profiles"])
    plan = discover(workspace["northwind"])
    pr, _ = store.resolve("northwind_industries", plan)
    pr.label_overrides["Assumptions"] = "other_income"   # deliberately odd, proves the path
    store.save(pr.confirm(by="test"))

    state = run_graph(str(workspace["pdf"]), str(workspace["northwind"]),
                      output_path=str(workspace["northwind_out"]), use_llm_mapping=False)
    assert state["status"] == "ok"
    assert store.get("northwind_industries").label_overrides["Assumptions"] == "other_income"


def test_json_is_human_editable(workspace):
    store = ProfileStore(workspace["profiles"])
    pr, _ = store.resolve("zenith_chemicals", discover(workspace["zenith"]))
    path = store.save(pr)
    raw = path.read_text()
    assert '"company_key": "zenith_chemicals"' in raw
    assert CompanyProfile.from_json(raw).company_key == "zenith_chemicals"
