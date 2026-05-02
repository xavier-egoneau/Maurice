#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAURICE_HOME="${MAURICE_HOME:-$HOME/.maurice}"
CONFIG_PATH="$MAURICE_HOME/config.yaml"
CLI_LINK="$HOME/.local/bin/maurice"
AUTOSTART_FILE="$HOME/.config/autostart/maurice.desktop"

ASSUME_YES=0
DELETE_WORKSPACE=0
KEEP_WORKSPACE=0
FORCE_WORKSPACE=0
DRY_RUN=0
WORKSPACE_OVERRIDE=""

info() { printf '\n\033[1;38;5;208m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
die() { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./uninstall.sh [options]

Supprime l'installation locale de Maurice pour refaire des tests propres.

Options:
  --yes                 Ne demande pas de confirmation pour ~/.maurice et l'autostart.
  --delete-workspace    Supprime aussi le workspace global lu dans ~/.maurice/config.yaml.
  --keep-workspace      Ne supprime jamais le workspace global.
  --workspace PATH      Utilise ce workspace au lieu de lire ~/.maurice/config.yaml.
  --force-workspace     Autorise la suppression d'un workspace large/protege.
  --dry-run             Affiche ce qui serait supprime sans rien supprimer.
  -h, --help            Affiche cette aide.

Par securite, le workspace global n'est pas supprime par --yes seul.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes) ASSUME_YES=1 ;;
    --delete-workspace) DELETE_WORKSPACE=1 ;;
    --keep-workspace) KEEP_WORKSPACE=1 ;;
    --workspace)
      [ "$#" -ge 2 ] || die "--workspace attend un chemin."
      WORKSPACE_OVERRIDE="$2"
      shift
      ;;
    --force-workspace) FORCE_WORKSPACE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Option inconnue: $1" ;;
  esac
  shift
done

if [ "$DELETE_WORKSPACE" -eq 1 ] && [ "$KEEP_WORKSPACE" -eq 1 ]; then
  die "--delete-workspace et --keep-workspace sont incompatibles."
fi

run_rm_rf() {
  local path="$1"
  if [ ! -e "$path" ] && [ ! -L "$path" ]; then
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'dry-run: rm -rf %s\n' "$path"
    return 0
  fi
  rm -rf "$path"
}

run_rm_f() {
  local path="$1"
  if [ ! -e "$path" ] && [ ! -L "$path" ]; then
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'dry-run: rm -f %s\n' "$path"
    return 0
  fi
  rm -f "$path"
}

confirm_delete() {
  local label="$1"
  local expected="$2"
  if [ "$ASSUME_YES" -eq 1 ]; then
    return 0
  fi
  printf '\n%s\n' "$label"
  printf 'Tape "%s" pour confirmer: ' "$expected"
  local answer
  IFS= read -r answer
  [ "$answer" = "$expected" ]
}

configured_workspace() {
  if [ ! -f "$CONFIG_PATH" ]; then
    return 0
  fi
  python3 - "$CONFIG_PATH" <<'PY' 2>/dev/null || true
from __future__ import annotations

import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
in_usage = False
usage_indent = 0

for raw in path.read_text(encoding="utf-8").splitlines():
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        continue
    indent = len(raw) - len(raw.lstrip(" "))
    if stripped == "usage:":
        in_usage = True
        usage_indent = indent
        continue
    if in_usage and indent <= usage_indent:
        in_usage = False
    if in_usage and stripped.startswith("workspace:"):
        value = stripped.split(":", 1)[1].strip()
        value = value.strip("\"'")
        if value:
            print(Path(os.path.expanduser(value)).resolve())
        break
PY
}

resolve_path() {
  python3 - "$1" <<'PY' 2>/dev/null || printf '%s\n' "$1"
from __future__ import annotations

import os
import sys
from pathlib import Path

print(Path(os.path.expanduser(sys.argv[1])).resolve())
PY
}

is_protected_workspace() {
  local workspace="$1"
  local resolved_home
  resolved_home="$(cd "$HOME" && pwd -P)"

  case "$workspace" in
    ""|"/"|"$resolved_home"|"$resolved_home/"|"$resolved_home/Documents"|"$resolved_home/Desktop"|"$resolved_home/Downloads")
      return 0
      ;;
  esac
  return 1
}

remove_cli_link_if_owned() {
  if [ ! -L "$CLI_LINK" ]; then
    return 0
  fi
  local target
  target="$(readlink -f "$CLI_LINK" 2>/dev/null || true)"
  case "$target" in
    "$ROOT_DIR"/*)
      run_rm_f "$CLI_LINK"
      ;;
    *)
      warn "Lien CLI conserve: $CLI_LINK pointe vers $target"
      ;;
  esac
}

stop_daemon_if_possible() {
  local workspace="$1"
  if ! command -v maurice >/dev/null 2>&1; then
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'dry-run: maurice stop%s\n' "${workspace:+ --workspace $workspace}"
    return 0
  fi
  if [ -n "$workspace" ]; then
    maurice stop --workspace "$workspace" >/dev/null 2>&1 || true
  else
    maurice stop >/dev/null 2>&1 || true
  fi
}

if [ -n "$WORKSPACE_OVERRIDE" ]; then
  WORKSPACE="$(resolve_path "$WORKSPACE_OVERRIDE")"
else
  WORKSPACE="$(configured_workspace)"
fi

info "Desinstallation de Maurice"
printf 'Etat global : %s\n' "$MAURICE_HOME"
if [ -n "$WORKSPACE" ]; then
  printf 'Workspace global detecte : %s\n' "$WORKSPACE"
else
  printf 'Workspace global detecte : aucun\n'
fi
printf 'Autostart : %s\n' "$AUTOSTART_FILE"
printf 'Lien CLI : %s\n' "$CLI_LINK"

if ! confirm_delete "Suppression de l'etat global, du lien CLI possede par ce repo et de l'autostart." "DELETE"; then
  die "Desinstallation annulee."
fi

stop_daemon_if_possible "$WORKSPACE"
run_rm_f "$AUTOSTART_FILE"
remove_cli_link_if_owned
run_rm_rf "$MAURICE_HOME"

if [ "$KEEP_WORKSPACE" -eq 0 ] && [ -n "$WORKSPACE" ] && [ -d "$WORKSPACE" ]; then
  if [ "$DELETE_WORKSPACE" -eq 0 ]; then
    if [ "$ASSUME_YES" -eq 0 ]; then
      printf '\nSupprimer aussi le workspace global ?\n  %s\n' "$WORKSPACE"
      printf 'Tape "DELETE WORKSPACE" pour confirmer, autre chose pour le conserver: '
      IFS= read -r answer
      if [ "$answer" = "DELETE WORKSPACE" ]; then
        DELETE_WORKSPACE=1
      fi
    fi
  fi

  if [ "$DELETE_WORKSPACE" -eq 1 ]; then
    if is_protected_workspace "$WORKSPACE" && [ "$FORCE_WORKSPACE" -eq 0 ]; then
      warn "Workspace conserve par securite: $WORKSPACE"
      warn "Relance avec --delete-workspace --force-workspace pour forcer cette suppression."
    else
      if [ "$ASSUME_YES" -eq 0 ]; then
        if ! confirm_delete "Derniere confirmation pour supprimer le workspace $WORKSPACE." "DELETE WORKSPACE"; then
          warn "Workspace conserve: $WORKSPACE"
          DELETE_WORKSPACE=0
        fi
      fi
      if [ "$DELETE_WORKSPACE" -eq 1 ]; then
        run_rm_rf "$WORKSPACE"
      fi
    fi
  fi
fi

info "Termine"
if [ "$DRY_RUN" -eq 1 ]; then
  printf 'Aucune suppression effectuee (--dry-run).\n'
fi
