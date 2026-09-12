#!/bin/sh
# fver remote installer. Usage:
#   curl -fsSL https://raw.githubusercontent.com/TheoOltean/fver/main/get-fver.sh | sh
# Environment overrides: FVER_REPO (git URL), FVER_REF (branch/tag), FVER_PYTHON (interpreter).
# POSIX sh; no sudo; works on Linux, macOS and the BSDs.
set -eu

FVER_REPO="${FVER_REPO:-https://github.com/TheoOltean/fver.git}"
FVER_REF="${FVER_REF:-main}"
BIN_DIR="$HOME/.local/bin"
ORIGINAL_PATH="$PATH"

say() { printf '%s\n' "$*"; }
die() { printf 'get-fver: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- downloader -----------------------------------------------------------
if have curl; then
  fetch() { curl -fsSL "$1"; }
elif have wget; then
  fetch() { wget -qO- "$1"; }
else
  die "need curl or wget"
fi

# --- python >= 3.11 -------------------------------------------------------
find_python() {
  for c in "${FVER_PYTHON:-}" python3.14 python3.13 python3.12 python3.11 python3 python; do
    [ -n "$c" ] || continue
    if have "$c" && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      printf '%s' "$c"; return 0
    fi
  done
  return 1
}
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  say "fver needs Python 3.11 or newer and none was found on PATH."
  case "$(uname -s)" in
    Darwin) say "  macOS:   brew install python  (or https://www.python.org/downloads/)" ;;
    Linux)
      say "  Debian/Ubuntu: sudo apt-get install python3 python3-venv"
      say "  Fedora:        sudo dnf install python3"
      say "  Arch:          sudo pacman -S python"
      say "  Alpine:        sudo apk add python3" ;;
    FreeBSD) say "  FreeBSD: sudo pkg install python3" ;;
    OpenBSD) say "  OpenBSD: doas pkg_add python3" ;;
    NetBSD)  say "  NetBSD:  sudo pkgin install python3" ;;
    *)       say "  install Python 3.11+ with your package manager" ;;
  esac
  say "Then re-run this installer (set FVER_PYTHON=/path/to/python3 if it is not on PATH)."
  exit 1
fi

# --- installer: uv, then pipx, then pip --user -----------------------------
mkdir -p "$BIN_DIR"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) PATH="$BIN_DIR:$PATH"; export PATH ;; esac

SPEC="git+${FVER_REPO}@${FVER_REF}"
if ! have uv; then
  say "installing uv into $BIN_DIR ..."
  fetch https://astral.sh/uv/install.sh | UV_INSTALL_DIR="$BIN_DIR" UV_NO_MODIFY_PATH=1 sh -s -- -q \
    || say "uv install failed; trying pipx / pip instead"
fi
if have uv; then
  say "installing fver with uv from $SPEC ..."
  uv tool install --force --python "$PY" "$SPEC"
elif have pipx; then
  say "installing fver with pipx from $SPEC ..."
  pipx install --force --python "$PY" "$SPEC"
else
  say "installing fver with pip --user from $SPEC ..."
  "$PY" -m pip install --user --upgrade "$SPEC" \
    || die "pip install failed (try: $PY -m ensurepip --user, or install uv: https://docs.astral.sh/uv/)"
fi

# --- PATH hint --------------------------------------------------------------
if ! have fver; then
  die "fver was installed but is not on PATH. Add $BIN_DIR to PATH (see below) and re-open your shell."
fi
if [ "$(command -v fver)" = "$BIN_DIR/fver" ]; then
  # Was BIN_DIR on the user's PATH before we prepended it?
  case ":$ORIGINAL_PATH:" in *":$BIN_DIR:"*) on_path=1 ;; *) on_path=0 ;; esac
  if [ "$on_path" -eq 0 ]; then
    SHELL_NAME="$(basename "${SHELL:-sh}")"
    say ""
    say "Make sure $BIN_DIR is on your PATH. For $SHELL_NAME add:"
    case "$SHELL_NAME" in
      fish) say "  fish_add_path $BIN_DIR" ;;
      *)    say "  export PATH=\"$BIN_DIR:\$PATH\"" ;;
    esac
    say "to your shell startup file, then open a new terminal."
  fi
fi

say ""
say "Installed: $(fver --version)"
# The toolchain (cbmc, and an opam switch with Frama-C, Alt-Ergo, Rocq, Iris,
# Cerberus and RefinedC) is not optional. FVER_SKIP_SETUP=1 is for fver's own CI.
if [ "${FVER_SKIP_SETUP:-}" = 1 ]; then
  say "FVER_SKIP_SETUP=1: not running 'fver setup'."
  say "Next: cd into a C repository and run 'fver init'."
else
  say "Installing the toolchain with 'fver setup' (the first run builds it: 30-60 min) ..."
  fver setup
fi
