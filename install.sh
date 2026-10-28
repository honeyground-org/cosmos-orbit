#!/bin/sh
# Install a private virtual environment and a launcher without changing shell configuration.
set -eu
LAUNCH=0
case "${1:-}" in "") ;; --launch) LAUNCH=1 ;; *) echo "Usage: ./install.sh [--launch]" >&2; exit 2 ;; esac
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}
INSTALL_DIR=${HARNESS_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/local-harness-app}
BIN_DIR=${HARNESS_BIN_DIR:-$HOME/.local/bin}
case "$INSTALL_DIR:$BIN_DIR" in /*:/*) ;; *) echo 'Installation paths must be absolute.' >&2; exit 1 ;; esac
command -v "$PYTHON" >/dev/null 2>&1 || { echo 'Python 3.9+ is required.' >&2; exit 1; }
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ is required"; import fcntl' || exit 1
if [ -e "$BIN_DIR/harness" ] || [ -L "$BIN_DIR/harness" ]; then
  TARGET=$(readlink "$BIN_DIR/harness" 2>/dev/null || true)
  if [ "$TARGET" != "$INSTALL_DIR/venv/bin/harness" ] && [ "$TARGET" != "$SOURCE_DIR/bin/harness" ]; then
    echo "Refusing to replace an unrelated launcher: $BIN_DIR/harness" >&2
    echo 'Set HARNESS_BIN_DIR to another absolute directory.' >&2
    exit 1
  fi
fi
mkdir -p "$INSTALL_DIR" "$BIN_DIR"
"$PYTHON" -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/python" -m pip install --disable-pip-version-check --upgrade "$SOURCE_DIR"
"$INSTALL_DIR/venv/bin/harness" --help >/dev/null
ln -sfn "$INSTALL_DIR/venv/bin/harness" "$BIN_DIR/harness"
printf '\nInstalled Local Harness. Run: %s/harness\n' "$BIN_DIR"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) printf 'Add this directory to your PATH: %s\n' "$BIN_DIR" ;; esac
printf 'Next: harness doctor\nNo worker, model call, or background service was started.\n'

if [ "$LAUNCH" = 1 ]; then
  exec "$BIN_DIR/harness"
fi
