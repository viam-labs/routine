#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

VENV_NAME="${VENV_NAME:-venv}"
python3 -m venv "$VENV_NAME"
"$VENV_NAME/bin/pip" install --upgrade pip
"$VENV_NAME/bin/pip" install -r requirements.txt
touch .installed
