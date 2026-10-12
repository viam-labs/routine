#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p dist
tar --exclude='./venv' --exclude='./.venv' --exclude='./dist' \
  --exclude='./__pycache__' --exclude='*.pyc' --exclude='./.pytest_cache' \
  --exclude='./.git' --exclude='./.installed' \
  -czf dist/archive.tar.gz \
  meta.json run.sh setup.sh build.sh requirements.txt \
  src README.md examples LICENSE
echo "Wrote dist/archive.tar.gz"
