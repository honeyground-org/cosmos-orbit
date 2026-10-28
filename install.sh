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
for LAUNCHER in cosmos-orbit harness; do
  if [ -e "$BIN_DIR/$LAUNCHER" ] || [ -L "$BIN_DIR/$LAUNCHER" ]; then
    TARGET=$(readlink "$BIN_DIR/$LAUNCHER" 2>/dev/null || true)
    if [ "$TARGET" != "$INSTALL_DIR/venv/bin/$LAUNCHER" ] && [ "$TARGET" != "$SOURCE_DIR/bin/$LAUNCHER" ]; then
      echo "Refusing to replace an unrelated launcher: $BIN_DIR/$LAUNCHER" >&2
      echo 'Set HARNESS_BIN_DIR to another absolute directory.' >&2
      exit 1
    fi
  fi
done
mkdir -p "$INSTALL_DIR" "$BIN_DIR"
"$PYTHON" -m venv "$INSTALL_DIR/venv"
WHEEL_DIR=$(mktemp -d)
trap 'rm -rf "$WHEEL_DIR"' EXIT HUP INT TERM
"$INSTALL_DIR/venv/bin/python" -m pip wheel --disable-pip-version-check --no-deps --wheel-dir "$WHEEL_DIR" "$SOURCE_DIR"
# Remove the legacy distribution so its uninstall record cannot delete the new shared package.
if "$INSTALL_DIR/venv/bin/python" -m pip show local-harness >/dev/null 2>&1; then
  "$INSTALL_DIR/venv/bin/python" -m pip uninstall --yes local-harness
fi
"$INSTALL_DIR/venv/bin/python" -m pip install --disable-pip-version-check --upgrade "$WHEEL_DIR"/cosmos_orbit-*.whl
"$INSTALL_DIR/venv/bin/cosmos-orbit" --help >/dev/null
for LAUNCHER in cosmos-orbit harness; do
  ln -sfn "$INSTALL_DIR/venv/bin/$LAUNCHER" "$BIN_DIR/$LAUNCHER"
done
printf '\nInstalled Cosmos Orbit. Run: %s/cosmos-orbit\n' "$BIN_DIR"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) printf 'Add this directory to your PATH: %s\n' "$BIN_DIR" ;; esac
printf 'Next: cosmos-orbit doctor\nNo worker, model call, or background service was started.\n'

rm -rf "$WHEEL_DIR"
trap - EXIT HUP INT TERM
if [ "$LAUNCH" = 1 ]; then
  exec "$BIN_DIR/cosmos-orbit"
fi
