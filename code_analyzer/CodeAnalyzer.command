#!/usr/bin/env bash
# Double-clickable launcher for the Code Analyzer local browser GUI.
#
# Resolves its own location so it works regardless of the current working
# directory it's launched from (Finder/Explorer double-click, or a symlink
# on the Desktop), prefers a project-local virtualenv if one exists, and
# falls back to running straight from source (PYTHONPATH=src) when the
# package hasn't been `pip install -e .`'d.
set -euo pipefail

# Resolve the directory this script lives in, following symlinks.
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR"

# Prefer a project-local virtualenv's python if present.
if [ -x "./venv/bin/python3" ]; then
  PYTHON="./venv/bin/python3"
elif [ -x "./.venv/bin/python3" ]; then
  PYTHON="./.venv/bin/python3"
else
  PYTHON="python3"
fi

# If the package isn't importable as-is (not pip-installed), fall back to
# running directly from source.
if ! "$PYTHON" -c "import codeanalyzer" >/dev/null 2>&1; then
  export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
fi

exec "$PYTHON" -m codeanalyzer.cli gui
