# Demo files for Copilot Studio (local server)

Drop one folder per company. FinScan reads from here when Copilot calls
`POST /demo/update` — no file upload in chat needed.

## Layout

```
test/
  tencent/
    Tencent_q325.pdf    ← results PDF (name should contain period, e.g. q325)
    model.xlsx          ← company Excel model (not *_updated*)
```

## Example Copilot prompt

> Return me an updated excel for Q325 for company name Tancent

Maps automatically:
- **Tancent** → `tencent` (typo alias)
- **Q325** → column header `3Q2025`, picks `Tencent_q325.pdf`

## Add another company

```bash
mkdir -p test/acme
cp ~/Downloads/acme_q4_results.pdf test/acme/
cp ~/Downloads/acme_model.xlsx test/acme/model.xlsx
finscan profiles confirm acme --sheets "P&L Summary"   # once, via CLI
```

Then in Copilot: "Update excel for Q4 for Acme"
