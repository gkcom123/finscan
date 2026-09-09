"""Graph wiring.

  ingest_pdf ─► discover ─► extract ─► resolve_profile ─► normalize ─► refine_mapping
                              ▲                                                    │
                              │                                          check_periods
                              │                                                    │
                              │                                               validate
                              │                                                    │
                              └──────────── prepare_retry ◄──── arithmetic errors, once ────┤
                                                                                           │
                                            report ◄─── write_excel ◄─── ok / needs_review ─┤
                                              ▲                                            │
                                              └──────────── awaiting_confirmation ──────────┘
                                                           (nothing is written)
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from finscan.config import settings
from finscan.graph import nodes
from finscan.graph.state import GraphState


def build_graph(checkpointer: Any = None):
    g = StateGraph(GraphState)

    g.add_node("ingest_pdf", nodes.ingest_pdf)
    g.add_node("discover", nodes.discover_workbook)
    g.add_node("extract", nodes.extract_financials)
    g.add_node("resolve_profile", nodes.resolve_profile)
    g.add_node("normalize", nodes.normalize)
    g.add_node("refine_mapping", nodes.refine_mapping)
    g.add_node("extract_label_rows", nodes.extract_label_rows)
    g.add_node("check_periods", nodes.check_periods)
    g.add_node("crosscheck", nodes.crosscheck_formulas)
    g.add_node("validate", nodes.validate_node)
    g.add_node("prepare_retry", nodes.prepare_retry)
    g.add_node("write_excel", nodes.write_excel)
    g.add_node("report", nodes.build_report)

    g.set_entry_point("ingest_pdf")
    g.add_edge("ingest_pdf", "discover")
    g.add_edge("discover", "extract")
    g.add_edge("extract", "resolve_profile")
    g.add_edge("resolve_profile", "normalize")
    g.add_edge("normalize", "refine_mapping")
    g.add_edge("refine_mapping", "extract_label_rows")
    g.add_edge("extract_label_rows", "check_periods")
    g.add_edge("check_periods", "crosscheck")
    g.add_edge("crosscheck", "validate")
    g.add_conditional_edges(
        "validate",
        nodes.route_after_validate,
        {"retry": "prepare_retry", "write": "write_excel", "hold": "write_excel"},
    )
    g.add_edge("prepare_retry", "extract")
    g.add_edge("write_excel", "report")
    g.add_edge("report", END)

    return g.compile(checkpointer=checkpointer)


def run(
    pdf_path: str,
    excel_path: str,
    output_path: str | None = None,
    company: str | None = None,
    sheets: list[str] | None = None,
    period_label: str | None = None,
    profile_store: str | None = None,
    require_confirmation: bool | None = None,
    use_llm_mapping: bool = True,
    reliable_labels: bool | None = None,
    dry_run: bool = False,
    allow_period_gap: bool = False,
) -> dict:
    app = build_graph()
    return app.invoke(
        {
            "pdf_path": pdf_path,
            "excel_path": excel_path,
            "output_path": output_path,
            "company_key_override": company,
            "sheets_override": sheets,
            "period_label_override": period_label,
            "profile_store": profile_store or str(settings.finscan_profile_store),
            "require_confirmation": (settings.finscan_require_confirmation
                                     if require_confirmation is None else require_confirmation),
            "allow_period_gap": allow_period_gap,
            "use_llm_mapping": use_llm_mapping,
            "reliable_labels": (settings.finscan_reliable_labels
                                if reliable_labels is None else reliable_labels),
            "dry_run": dry_run,
            "hint": "",
            "issues": [],
            "retry_count": 0,
        }
    )
