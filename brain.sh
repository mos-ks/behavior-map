#!/bin/sh
# Behaviour map, https://github.com/mos-ks/behavior-map. Copyright (c) 2026 Dr. Moussa Kassem Sbeyti. MIT licence, see LICENSE.
# Opens the behaviour map page in the browser. The page runs in the background and stops by itself when its
# tab is closed.
cd "$(dirname "$0")" || exit 1
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
exec "$PY" ui.py --background "$@"
