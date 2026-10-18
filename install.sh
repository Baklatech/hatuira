#!/bin/bash

# hatuira installer for Arch and Debian-based distros, with Omarchy integration.
#
#   ./install.sh                 install from this checkout
#   curl -fsSL https://raw.githubusercontent.com/Baklatech/hatuira/main/install.sh | bash
#   ./install.sh --uninstall     remove hatuira and everything the installer added
#
# Options:
#   --prefix=DIR      where to install (default: ~/.local, command in DIR/bin)
#   --no-deps         don't install python3 (and curl, tar for a remote install)
#   --no-menu         don't add hatuira to the Omarchy menu
#   --no-hook         don't repaint lights when the Omarchy theme changes
#   --uninstall       remove hatuira; settings in ~/.config/hatuira are kept

set -euo pipefail

REPO="${HATUIRA_REPO:-Baklatech/hatuira}"
PREFIX="${PREFIX:-$HOME/.local}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"

WANT_DEPS=true
WANT_MENU=true
WANT_HOOK=true
UNINSTALL=false

MARK_OPEN="// >>> hatuira >>>"
MARK_CLOSE="// <<< hatuira <<<"

# --- output ---------------------------------------------------------------

if [[ -t 1 ]]; then
  C_ACCENT=$'\e[1;34m' C_OK=$'\e[32m' C_WARN=$'\e[33m' C_ERR=$'\e[31m' C_RESET=$'\e[0m'
else
  C_ACCENT="" C_OK="" C_WARN="" C_ERR="" C_RESET=""
fi

step() { echo "${C_ACCENT}::${C_RESET} $*"; }
ok() { echo "   ${C_OK}✓${C_RESET} $*"; }
warn() { echo "   ${C_WARN}!${C_RESET} $*"; }
die() {
  echo "${C_ERR}✗ $*${C_RESET}" >&2
  exit 1
}

for arg in "$@"; do
  case "$arg" in
  --prefix=*) PREFIX="${arg#--prefix=}" ;;
  --no-deps) WANT_DEPS=false ;;
  --no-menu) WANT_MENU=false ;;
  --no-hook) WANT_HOOK=false ;;
  --uninstall) UNINSTALL=true ;;
  -h | --help)
    sed -n '3,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *) die "unknown option: $arg (see --help)" ;;
  esac
done

BIN_DIR="$PREFIX/bin"
BIN="$BIN_DIR/hatuira"
SHARE="$PREFIX/share/hatuira"
DESKTOP="$DATA_DIR/applications/hatuira.desktop"
OMARCHY="$CONFIG_DIR/omarchy"
MENU="${OMARCHY_MENU:-$OMARCHY/extensions/omarchy-menu.jsonc}"
HOOK="$OMARCHY/hooks/theme-set.d/hatuira"

# --- system ---------------------------------------------------------------

SUDO=""
if (( EUID != 0 )); then
  SUDO="sudo"
fi

have() { command -v "$1" >/dev/null 2>&1; }

detect_distro() {
  DISTRO="other"
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case " ${ID:-} ${ID_LIKE:-} " in
    *" arch "* | *" archlinux "* | *" manjaro "* | *" endeavouros "*) DISTRO="arch" ;;
    *" debian "* | *" ubuntu "*) DISTRO="debian" ;;
    esac
  fi
}

is_omarchy() {
  have omarchy-launch-or-focus-tui
}

# The directory this script sits in when it is run from a checkout; empty when piped.
script_dir() {
  local src="${BASH_SOURCE[0]:-}"
  if [[ -n $src && -f $src ]]; then
    cd "$(dirname "$src")" && pwd
  fi
}

python_ok() {
  have python3 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null
}

# Debian and Ubuntu split parts of the standard library into separate packages.
stdlib_ok() {
  python3 -c 'import ssl, termios, json, urllib.request' 2>/dev/null
}

install_deps() {
  step "Dependencies"
  local missing=() c
  if [[ -z $SRC ]]; then
    for c in curl tar; do
      have "$c" || missing+=("$c")
    done
  fi
  if ! have python3 || ! stdlib_ok; then
    [[ $DISTRO == "arch" ]] && missing+=("python") || missing+=("python3")
  fi
  if (( ${#missing[@]} > 0 )); then
    if ! $WANT_DEPS; then
      die "missing: ${missing[*]} (run without --no-deps to install them)"
    fi
    case "$DISTRO" in
    arch)
      # A machine that has never synced has no package database yet.
      $SUDO pacman -S --needed --noconfirm "${missing[@]}" 2>/dev/null ||
        $SUDO pacman -Sy --needed --noconfirm "${missing[@]}"
      ;;
    debian)
      $SUDO apt-get update -qq
      $SUDO apt-get install -y -qq ca-certificates "${missing[@]}"
      ;;
    *) die "please install: ${missing[*]}" ;;
    esac
  fi
  if ! python_ok; then
    die "hatuira needs Python 3.10 or newer, found $(python3 -V 2>&1) (Debian 12+, Ubuntu 22.04+)"
  fi
  stdlib_ok || die "this Python lacks ssl or termios"
  ok "$(python3 -V 2>&1)"
}

# --- getting the files ----------------------------------------------------

fetch_source() {
  step "Downloading hatuira"
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" |
    tar -xz -C "$TMP" --strip-components=1 || die "could not download $REPO"
  [[ -f $TMP/hatuira/cli.py ]] || die "unexpected contents in $REPO"
  SRC="$TMP"
}

install_files() {
  step "Installing"
  mkdir -p "$SHARE/bin" "$BIN_DIR"
  rm -rf "$SHARE/hatuira"
  cp -R "$SRC/hatuira" "$SHARE/hatuira"
  find "$SHARE/hatuira" -name __pycache__ -type d -prune -exec rm -rf {} +
  install -m755 "$SRC/bin/hatuira" "$SHARE/bin/hatuira"
  ln -sf "$SHARE/bin/hatuira" "$BIN"
  ok "hatuira → $BIN"
}

# --- launcher -------------------------------------------------------------

install_desktop() {
  local exec_line="$BIN" terminal="true"
  if is_omarchy; then # an Omarchy-styled window that is focused when already open
    exec_line="omarchy-launch-or-focus-tui $BIN"
    terminal="false"
  fi
  mkdir -p "$(dirname "$DESKTOP")"
  cat >"$DESKTOP" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=hatuira
GenericName=Home Assistant
Comment=Home Assistant lights, switches and covers in the terminal
Comment[ru]=Свет, выключатели и шторы Home Assistant в терминале
Exec=$exec_line
Terminal=$terminal
Icon=preferences-desktop-color
Categories=Utility;
Keywords=home;assistant;light;lamp;cover;smart;
EOF
  have update-desktop-database && update-desktop-database -q "$(dirname "$DESKTOP")" 2>/dev/null || true
  ok "app launcher entry → $DESKTOP"
}

# --- Omarchy --------------------------------------------------------------

menu_remove() {
  [[ -f $MENU ]] && grep -qF "$MARK_OPEN" "$MENU" || return 0
  sed "\\|$MARK_OPEN|,\\|$MARK_CLOSE|d" "$MENU" >"$MENU.tmp" && mv "$MENU.tmp" "$MENU"
}

menu_add() {
  [[ -f $MENU ]] || { mkdir -p "$(dirname "$MENU")" && printf '{\n}\n' >"$MENU"; }
  if ! grep -q '^[[:space:]]*{[[:space:]]*$' "$MENU"; then
    warn "could not find where to add a row in $MENU — left untouched"
    return 0
  fi
  local row="  \"hatuira\": {\"icon\": \"󰌵\", \"label\": \"Hatuira\", \"aliases\": [\"hatuira\", \"ha\", \"light\", \"home\"], \"description\": \"hatuira — свет и шторы Home Assistant\", \"action\": \"omarchy-launch-or-focus-tui $BIN\"},"
  if grep -qF "$MARK_OPEN" "$MENU"; then
    # Already there: refresh the row where it stands, so a reinstall keeps your ordering.
    awk -v head="$MARK_OPEN" -v row="$row" -v tail="$MARK_CLOSE" '
      index($0, head) { print; print row; skip = 1; next }
      index($0, tail) { skip = 0 }
      !skip { print }
    ' "$MENU" >"$MENU.tmp"
  else
    cp "$MENU" "$MENU.bak.$(date +%s)"
    awk -v head="$MARK_OPEN" -v row="$row" -v tail="$MARK_CLOSE" '
      { print }
      !done && /^[[:space:]]*\{[[:space:]]*$/ { print head; print row; print tail; done = 1 }
    ' "$MENU" >"$MENU.tmp"
  fi
  if grep -qF "$MARK_CLOSE" "$MENU.tmp"; then
    mv "$MENU.tmp" "$MENU"
    ok "Omarchy menu → Hatuira"
  else
    rm -f "$MENU.tmp"
    warn "could not update $MENU — left untouched"
  fi
}

hook_add() {
  mkdir -p "$(dirname "$HOOK")"
  cat >"$HOOK" <<EOF
#!/bin/bash

# hatuira: after an Omarchy theme change, repaint the Home Assistant lights chosen with \`s\`.
"$BIN" sync >/dev/null 2>&1 &
EOF
  chmod 755 "$HOOK"
  ok "theme hook → $HOOK"
}

install_omarchy() {
  is_omarchy || return 0
  step "Omarchy"
  if $WANT_MENU; then menu_add; fi
  if $WANT_HOOK; then hook_add; fi
}

# --- main -----------------------------------------------------------------

if $UNINSTALL; then
  rm -rf "$SHARE"
  rm -f "$BIN" "$DESKTOP" "$HOOK"
  menu_remove
  ok "hatuira removed (settings kept in $CONFIG_DIR/hatuira)"
  exit 0
fi

detect_distro
SRC=$(script_dir)
[[ -n $SRC && -f $SRC/hatuira/cli.py ]] || SRC=""

install_deps
[[ -n $SRC ]] || fetch_source
install_files
install_desktop
install_omarchy

step "Done"
case ":$PATH:" in
*":$BIN_DIR:"*) echo "   run: hatuira   — the first start asks for the address and token (hatuira --demo needs neither)" ;;
*)
  warn "$BIN_DIR is not in PATH — add it:  export PATH=\"$BIN_DIR:\$PATH\""
  echo "   then run: hatuira   (or right now: $BIN)"
  ;;
esac
