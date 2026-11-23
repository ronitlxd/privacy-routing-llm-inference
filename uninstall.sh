#!/usr/bin/env bash
# Privacy Routing for LLM Inference: uninstall.
#
#   ./uninstall.sh          stop the site, remove its user service, release the models it was using from memory,
#                           then ask (default no) before deleting .venv/logs/certs, and again before deleting
#                           driver profiles, .env (API keys) and models.toml
#   ./uninstall.sh --yes    the same without questions: stops and releases everything, deletes nothing
#
# Never deleted: model files, the model servers themselves (Ollama, vLLM ...), anything outside this folder.
# Never runs sudo: to stop a system model server, run it yourself (e.g. sudo systemctl stop ollama).
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

case "${1:-}" in
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    ""|--yes) ;;
    *) printf '  [ -- ] Unknown option: %s\n' "$1" >&2; exit 1 ;;
esac

# The uninstaller needs only the standard library: use the project's Python if it is still there.
if [ -x .venv/bin/python ]; then PY=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then PY=$(command -v python3)
else printf '  [ -- ] Python 3 is needed to read models.toml.\n' >&2; exit 1
fi
exec "$PY" jlr_setup.py --uninstall "$@"
