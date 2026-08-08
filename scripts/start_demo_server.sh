#!/usr/bin/env bash
# Start FinScan API for Copilot Studio local demo.
# Usage: ./scripts/start_demo_server.sh
# Then expose with ngrok:  ngrok http 8080

set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  echo "Run first: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && pip install -e ."
  exit 1
fi

source .venv/bin/activate

if [[ ! -f .env ]]; then
  echo "Copy .env.example to .env and set OPENAI_API_KEY + FINSCAN_API_KEY"
  exit 1
fi

echo "FinScan demo API on http://127.0.0.1:8080"
echo "  Health:    curl http://127.0.0.1:8080/health"
echo "  Swagger:   http://127.0.0.1:8080/docs"
echo "  Test dir:  ./test/  (see test/README.md)"
echo ""
echo "Expose to Copilot Studio (requires HTTPS):"
echo "  ngrok http 8080"
echo "  → paste the https URL host into docs/copilot_connector.yaml"
echo ""

exec uvicorn finscan.api.main:app --host 0.0.0.0 --port 8080 --reload
