import sys
from collections import Counter
from finscan.excel.discovery import discover
from finscan.profiles import ProfileStore
from finscan.graph.nodes import refine_mapping

xlsx, key = sys.argv[1], sys.argv[2]
plan = discover(xlsx)
profile = ProfileStore("profiles").get(key)
state = {"plan": plan, "profile": profile,
         "enabled_sheets": sorted(profile.enabled_sheets()) if profile else None,
         "use_llm_mapping": True, "issues": []}
out = refine_mapping(state)

for sheet in plan.in_scope:
    print(f"--- {sheet.sheet}")
    for rp in sheet.rows:
        if rp.field or rp.label_only:
            how = rp.match_method if rp.field else "LABEL FALLBACK"
            print(f"  r{rp.row:<4} {how:<14} {rp.label[:38]:<38} -> {rp.field or '(raw caption)'}")
print()
print(Counter(rp.match_method for s in plan.in_scope for rp in s.rows if rp.field))
for i in out["issues"]:
    print(f"[{i.severity}] {i.code}: {i.message}")
