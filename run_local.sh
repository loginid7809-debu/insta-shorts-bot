#!/usr/bin/env bash
# Convenience wrapper for local runs: loads .env then forwards all arguments.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; source .env; set +a; fi
exec python3 main.py "$@"
