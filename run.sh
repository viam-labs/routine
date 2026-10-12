#!/bin/bash
cd "$(dirname "$0")"

VENV_NAME="${VENV_NAME:-venv}"
PYTHON="$VENV_NAME/bin/python"

if [ ! -f .installed ] || [ ! -x "$PYTHON" ] || ! "$PYTHON" -c "import viam" >/dev/null 2>&1; then
  ./setup.sh || {
    echo "[routine] setup.sh failed" >&2
    exit 1
  }
fi

export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m main "$@"
