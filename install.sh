#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info() { printf '\n\033[1;38;5;208m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }

find_python() {
  for candidate in python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      "$candidate" - <<'PY'
import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
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
  if   command -v apt-get >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
  elif command -v dnf     >/dev/null 2>&1; then sudo dnf install -y python3 python3-pip
  elif command -v brew    >/dev/null 2>&1; then brew install python@3.12
  else warn "Je ne sais pas installer Python automatiquement sur ce système."; fi
}

ensure_python() {
  if PYTHON_BIN="$(find_python)"; then printf '%s\n' "$PYTHON_BIN"; return; fi
  try_install_python
  if PYTHON_BIN="$(find_python)"; then printf '%s\n' "$PYTHON_BIN"; return; fi
  echo "Python 3.12+ est requis." >&2; exit 1
}

PYTHON_BIN="$(ensure_python)"
cd "$ROOT_DIR"

info "Installation de Maurice"
if ! "$PYTHON_BIN" -m venv .venv >/dev/null 2>&1; then
  warn "Module venv manquant. Tentative d'installation."
  command -v apt-get >/dev/null 2>&1 && sudo apt-get install -y python3-venv
  "$PYTHON_BIN" -m venv .venv
fi

. .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e ".[dev]"

mkdir -p "$HOME/.local/bin"
ln -sf "$ROOT_DIR/.venv/bin/maurice" "$HOME/.local/bin/maurice"

info "Vérification"
python -c "import maurice; print('  maurice', maurice.__version__, '✓')"

cat <<'EOF'

  Maurice est installé.

  Lance-le dans n'importe quel dossier :

    cd mon-projet/
    maurice

  La configuration se fera au premier lancement.
EOF
