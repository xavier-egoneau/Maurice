#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="${WORKSPACE:-$HOME/Documents/workspace_maurice}"
PROFILE="${PROFILE:-limited}"

info() {
  printf '\n\033[1;38;5;208m%s\033[0m\n' "$1"
}

warn() {
  printf '\033[33m%s\033[0m\n' "$1"
}

find_python() {
  for candidate in python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      "$candidate" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
      if [ "$?" -eq 0 ]; then
        command -v "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

try_install_python() {
  if command -v apt-get >/dev/null 2>&1; then
    info "Python 3.12 introuvable. Tentative d'installation via apt."
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv python3-pip
    return
  fi
  if command -v dnf >/dev/null 2>&1; then
    info "Python 3.12 introuvable. Tentative d'installation via dnf."
    sudo dnf install -y python3 python3-pip
    return
  fi
  if command -v brew >/dev/null 2>&1; then
    info "Python 3.12 introuvable. Tentative d'installation via Homebrew."
    brew install python@3.12
    return
  fi
  warn "Je ne sais pas installer Python automatiquement sur ce systeme."
}

ensure_python() {
  if PYTHON_BIN="$(find_python)"; then
    printf '%s\n' "$PYTHON_BIN"
    return
  fi
  try_install_python
  if PYTHON_BIN="$(find_python)"; then
    printf '%s\n' "$PYTHON_BIN"
    return
  fi
  cat >&2 <<'EOF'
Python 3.12+ est requis.
Installe Python 3.12, puis relance:
  ./install.sh
EOF
  exit 1
}

PYTHON_BIN="$(ensure_python)"

cd "$ROOT_DIR"

info "Installation de Maurice"
if ! "$PYTHON_BIN" -m venv .venv >/dev/null 2>&1; then
  warn "Le module venv manque. Tentative d'installation du support venv."
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get install -y python3-venv
  fi
  "$PYTHON_BIN" -m venv .venv
fi

. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

mkdir -p "$HOME/.local/bin"
ln -sf "$ROOT_DIR/.venv/bin/maurice" "$HOME/.local/bin/maurice"

info "Verification locale"
maurice install

info "Onboarding Maurice"
maurice onboard --interactive --workspace "$WORKSPACE" --permission-profile "$PROFILE"
maurice doctor --workspace "$WORKSPACE"

cat <<EOF

Maurice est pret.

Demarrer le bot et les automatismes:
  maurice start

Ouvrir le dashboard:
  maurice dashboard
EOF
