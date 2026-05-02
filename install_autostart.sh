#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSTART_DIR="$HOME/.config/autostart"
AUTOSTART_FILE="$AUTOSTART_DIR/maurice.desktop"

NO_BROWSER=0
REMOVE=0
WORKSPACE=""
MAURICE_BIN=""

info() { printf '\n\033[1;38;5;208m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
die() { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./install_autostart.sh [options]

Installe une entree de demarrage de session Linux pour lancer Maurice.

Options:
  --workspace PATH   Lance Maurice avec ce workspace explicite.
  --no-browser      Lance le daemon sans ouvrir le navigateur.
  --maurice PATH    Chemin explicite vers l'executable maurice.
  --remove          Supprime l'entree autostart.
  -h, --help        Affiche cette aide.

Sans --workspace, la commande lance `maurice start` et suppose que Maurice est
configure en mode global.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --workspace)
      [ "$#" -ge 2 ] || die "--workspace attend un chemin."
      WORKSPACE="$2"
      shift
      ;;
    --no-browser) NO_BROWSER=1 ;;
    --maurice)
      [ "$#" -ge 2 ] || die "--maurice attend un chemin."
      MAURICE_BIN="$2"
      shift
      ;;
    --remove) REMOVE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Option inconnue: $1" ;;
  esac
  shift
done

if [ "$REMOVE" -eq 1 ]; then
  rm -f "$AUTOSTART_FILE"
  info "Autostart Maurice supprime"
  printf '%s\n' "$AUTOSTART_FILE"
  exit 0
fi

if [ -z "$MAURICE_BIN" ]; then
  if command -v maurice >/dev/null 2>&1; then
    MAURICE_BIN="$(command -v maurice)"
  elif [ -x "$HOME/.local/bin/maurice" ]; then
    MAURICE_BIN="$HOME/.local/bin/maurice"
  elif [ -x "$ROOT_DIR/.venv/bin/maurice" ]; then
    MAURICE_BIN="$ROOT_DIR/.venv/bin/maurice"
  else
    die "Executable maurice introuvable. Lance ./install.sh ou passe --maurice /chemin/maurice."
  fi
fi

if [ ! -x "$MAURICE_BIN" ]; then
  die "Executable maurice non executable: $MAURICE_BIN"
fi

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

desktop_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}

COMMAND="$(shell_quote "$MAURICE_BIN") start"
if [ -n "$WORKSPACE" ]; then
  COMMAND="$COMMAND --workspace $(shell_quote "$WORKSPACE")"
fi
if [ "$NO_BROWSER" -eq 1 ]; then
  COMMAND="$COMMAND --no-browser"
fi

mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Maurice
Comment=Start Maurice assistant
Exec=/bin/sh -lc $(desktop_quote "exec $COMMAND")
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

chmod 0644 "$AUTOSTART_FILE"

info "Autostart Maurice installe"
printf '%s\n' "$AUTOSTART_FILE"
printf 'Commande: %s\n' "$COMMAND"
if [ -z "$WORKSPACE" ]; then
  warn "Sans --workspace, Maurice doit etre configure en mode global."
fi
