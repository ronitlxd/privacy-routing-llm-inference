#!/usr/bin/env bash
# Privacy Routing for LLM Inference: one-command install on an Ubuntu server.
#
#   ./install.sh              guided install (about 10 short questions; Enter accepts each default, b goes back)
#   ./install.sh --start      start the site in the background (it is never set to start after a reboot)
#   ./install.sh --stop       stop the site
#   ./install.sh --check      check everything: models reachable, keys present, privacy rules met
#   ./install.sh --models     change where the models run
#   ./install.sh --set-key    change a cloud API key (hidden prompt, tested)
#   ./install.sh --access     switch between SSH-tunnel-only and HTTPS on the network
#   ./install.sh --update     git pull, reinstall the Python packages, restart the site if it is running
#   ./install.sh --yes --small-model NAME [--large-model NAME] [--preset local|cloud]
#                             no questions; models are named (there are no default models); cloud needs its key set
#
# This script never runs sudo and never changes the firewall or system services: when something needs an
# administrator, it prints the exact command for you to run.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
VENV=.venv
PY="$VENV/bin/python"

say()  { printf '  %s\n' "$*"; }
fail() { printf '\n  [ -- ] %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; }

find_python() {
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            command -v "$candidate"; return 0
        fi
    done
    return 1
}

ensure_venv() {
    if [ ! -x "$PY" ]; then
        local system_python
        system_python=$(find_python) || fail "Python 3.11 or newer is needed. Ask an administrator to run:
         sudo apt install python3 python3-venv"
        say "[ ok ] $("$system_python" --version) at $system_python"
        say "Creating the Python environment in $VENV ..."
        "$system_python" -m venv "$VENV" 2>/dev/null || {
            rm -rf "$VENV"
            fail "Python cannot create a virtual environment here. Ask an administrator to run:
         sudo apt install python3-venv"
        }
    fi
    # (Re)install the packages when requirements.txt changed since the last install.
    local want have=""
    want=$(sha256sum requirements.txt | cut -d' ' -f1)
    [ -f "$VENV/.requirements.sha256" ] && have=$(cat "$VENV/.requirements.sha256")
    if [ "$want" != "$have" ]; then
        say "Installing the Python packages (requirements.txt) ..."
        "$PY" -m pip install --quiet --disable-pip-version-check --upgrade pip
        "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt ||
            fail "pip could not install requirements.txt (see the message above)."
        echo "$want" > "$VENV/.requirements.sha256"
    fi
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --update)
        command -v git >/dev/null 2>&1 || fail "git is needed for --update: sudo apt install git"
        say "Updating the code (git pull) ..."
        git pull --ff-only || fail "git pull failed: local changes or a diverged branch; nothing was changed."
        ensure_venv
        exec "$PY" jlr_setup.py --restart-if-running ;;
    "") ensure_venv; exec "$PY" jlr_setup.py --install ;;
    --yes|--preset|--small-model|--large-model) ensure_venv; exec "$PY" jlr_setup.py --install "$@" ;;
    --start|--stop|--check|--models|--set-key|--access) ensure_venv; exec "$PY" jlr_setup.py "$@" ;;
    *) usage; fail "Unknown option: $1" ;;
esac
