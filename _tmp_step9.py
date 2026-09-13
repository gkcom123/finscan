import sys
from finscan.extract.pdf_reader import read_pdf
from finscan.extract.extractor import extract
from finscan.excel.discovery import discover
from finscan.profiles import ProfileStore
from finscan.graph.nodes import refine_mapping, extract_label_rows

pdf, xlsx, key = sys.argv[1], sys.argv[2], sys.argv[3]
doc = read_pdf(pdf)
ex, _ = extract(doc.as_prompt_text(),
                statements_text=doc.as_prompt_text(statements_only=True))
plan, profile = discover(xlsx), ProfileStore("profiles").get(key)
state = {"plan": plan, "profile": profile, "extraction": ex,
         "enabled_sheets": sorted(profile.enabled_sheets()) if profile else None,
         "statements_text": doc.as_prompt_text(statements_only=True),
         "use_llm_mapping": True, "reliable_labels": True, "issues": []}
refine_mapping(state)
out = extract_label_rows(state)

for label, v in sorted(out["label_values"].items()):
    print(f"  {label[:44]:<44} {v:>20,.2f}")
for i in out["issues"]:
    print(f"[{i.severity}] {i.code}: {i.message}")
