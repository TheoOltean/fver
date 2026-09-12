#!/bin/sh
# Install fver from this checkout onto PATH as an isolated, editable tool.
# Prefers uv, falls back to pipx. POSIX sh. For a one-line remote install use
# get-fver.sh instead.
set -eu
cd "$(dirname "$0")"
BIN_DIR="$HOME/.local/bin"
if command -v uv >/dev/null 2>&1; then
  uv tool install --force --editable .
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force --editable .
else
  echo "Install uv (https://docs.astral.sh/uv/) or pipx first." >&2
  exit 1
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    SHELL_NAME="$(basename "${SHELL:-sh}")"
    echo ""
    echo "Add $BIN_DIR to your PATH. For $SHELL_NAME:"
    case "$SHELL_NAME" in
      fish) echo "  fish_add_path $BIN_DIR" ;;
      *)    echo "  export PATH=\"$BIN_DIR:\$PATH\"" ;;
    esac
    ;;
esac
if [ "${FVER_SKIP_SETUP:-}" = 1 ]; then
  echo "FVER_SKIP_SETUP=1: not running 'fver setup'."
else
  echo "Installing the toolchain with 'fver setup' (first run: 20-40 min) ..."
  "$BIN_DIR/fver" setup || fver setup
fi
echo "Installed. Run 'fver --help'."
