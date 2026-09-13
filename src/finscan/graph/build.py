"""Graph wiring.

  ingest_pdf ─► discover ─► extract ─► resolve_profile ─► normalize
                              ▲                                  │
                              │                    resolve_cumulative_periods
                              │                                  │
                              │                          refine_mapping
                              │                                  │
                              │                        extract_label_rows
                              │                                  │
                              │                          check_periods
                              │                                  │
                              │                               validate
                              │                                    │
                              └──────────── prepare_retry ◄──── arithmetic errors, once ────┤
                                                                                           │
                                            report ◄─── write_excel ◄─── ok / needs_review ─┤
                                              ▲                                            │
                                              └──────────── awaiting_confirmation ──────────┘
                                                           (nothing is written)

resolve_cumulative_periods sits right after normalize and before everything else: it must
run before crosscheck simulates the sheet's own formulas (otherwise it would check a
still-cumulative D&A, say, against the filing) and before derive_missing (folded into this
same node, moved out of normalize) computes anything from that field — and it must run
before refine_mapping/extract_label_rows so the review report's value column shows the
corrected figure too, not just the write step.
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from finscan.config import settings
from finscan.graph import nodes
from finscan.graph.state import GraphState


#: Graph nodes in execution order. The linear path is 13 nodes; prepare_retry only
#: runs when validate routes back to extract, so it is excluded from the progress
#: denominator and simply reported as an extra phase if it fires.
_NODES: list[tuple[str, str]] = [
    ("ingest_pdf", "ingest_pdf"),
    ("discover", "discover_workbook"),
    ("extract", "extract_financials"),
    ("resolve_profile", "resolve_profile"),
    ("normalize", "normalize"),
    ("resolve_cumulative_periods", "resolve_cumulative_periods"),
    ("refine_mapping", "refine_mapping"),
    ("extract_label_rows", "extract_label_rows"),
    ("check_periods", "check_periods"),
    ("crosscheck", "crosscheck_formulas"),
    ("validate", "validate_node"),
    ("prepare_retry", "prepare_retry"),
    ("write_excel", "write_excel"),
    ("report", "build_report"),
]

#: Phases counted for the "[n/total]" progress counter.
PIPELINE_PHASES = len(_NODES) - 1


def build_graph(checkpointer: Any = None, printer: Any = None):
    """Compile the pipeline.

    `printer` is an optional console.ProgressPrinter; when supplied every node is
    wrapped so it announces itself and reports the issues it raised. Passing None
    (the default, and what the API uses) leaves the nodes completely untouched, so
    progress reporting cannot alter pipeline behaviour.
    """
    g = StateGraph(GraphState)

    for node_name, fn_name in _NODES:
        fn = getattr(nodes, fn_name)
        if printer is not None:
            from finscan.console import instrument
            fn = instrument(node_name, fn, printer)
        g.add_node(node_name, fn)

    g.set_entry_point("ingest_pdf")
    g.add_edge("ingest_pdf", "discover")
    g.add_edge("discover", "extract")
    g.add_edge("extract", "resolve_profile")
    g.add_edge("resolve_profile", "normalize")
    g.add_edge("normalize", "resolve_cumulative_periods")
    g.add_edge("resolve_cumulative_periods", "refine_mapping")
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
    printer: Any = None,
) -> dict:
    app = build_graph(printer=printer)
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
