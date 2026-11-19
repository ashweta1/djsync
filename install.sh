#!/usr/bin/env bash
# Install or update djsync.
#
#   curl -fsSL https://raw.githubusercontent.com/ashweta1/djsync/main/install.sh | bash
#
# What it does (nothing outside your home folder, no admin password):
#   - downloads djsync into ~/.djsync/app
#   - creates its own Python environment in ~/.djsync/venv and installs djsync there
#   - adds a double-clickable "djsync" launcher to your Desktop
#   - adds a `djsync` command in ~/.local/bin
# Your settings and track list in ~/.djsync are kept when you update.
# Run it again any time to update to the latest version.

set -euo pipefail

REPO="${DJSYNC_REPO:-ashweta1/djsync}"
BRANCH="${DJSYNC_BRANCH:-main}"
TARBALL="${DJSYNC_TARBALL:-https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH}"
BASE="$HOME/.djsync"
APP="$BASE/app"
VENV="$BASE/venv"
BIN="$HOME/.local/bin"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

# ---- 1. Python 3.11 or newer --------------------------------------------------------
find_python() {
  for py in python3.13 python3.12 python3.11 python3 \
            /opt/homebrew/bin/python3 /usr/local/bin/python3 \
            /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
    if command -v "$py" >/dev/null 2>&1 && \
       "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$py"; return 0
    fi
  done
  return 1
}
PY="$(find_python)" || die "djsync needs Python 3.11 or newer.
    Install it from https://www.python.org/downloads/ (click the big yellow button,
    open the downloaded file and follow the steps), then run this installer again."
say "Using $("$PY" --version) at $PY"

# ---- 2. Download djsync ---------------------------------------------------------------
mkdir -p "$BASE"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
say "Downloading djsync"
if [[ -f "$TARBALL" ]]; then
  cp "$TARBALL" "$TMP/djsync.tar.gz"
else
  curl -fsSL "$TARBALL" -o "$TMP/djsync.tar.gz" || die "Couldn't download djsync from $TARBALL"
fi
mkdir "$TMP/src"
tar -xzf "$TMP/djsync.tar.gz" -C "$TMP/src"
SRC="$(find "$TMP/src" -mindepth 1 -maxdepth 2 -name pyproject.toml -exec dirname {} \; | head -1)"
[[ -n "$SRC" ]] || die "The download doesn't look like djsync (no pyproject.toml)."
rm -rf "$APP"
mv "$SRC" "$APP"

# ---- 3. Python environment + install -------------------------------------------------------
if [[ ! -x "$VENV/bin/python" ]]; then
  say "Creating djsync's own Python environment"
  "$PY" -m venv "$VENV"
fi
say "Installing (this takes a minute the first time)"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet --upgrade "$APP"
"$VENV/bin/djsync" --help >/dev/null || die "djsync didn't install correctly."

# ---- 4. Launchers ----------------------------------------------------------------------
mkdir -p "$BIN"
ln -sf "$VENV/bin/djsync" "$BIN/djsync"

if [[ -d "$HOME/Desktop" ]]; then
  LAUNCHER="$HOME/Desktop/djsync.command"
  cat > "$LAUNCHER" <<EOF
#!/bin/bash
# Double-click to open djsync. Keep this window open while you use it.
exec "$VENV/bin/djsync"
EOF
  chmod +x "$LAUNCHER"
  say "Added a djsync launcher to your Desktop"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) warn "To use the \`djsync\` command in Terminal, add this line to ~/.zshrc:
       export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

cat <<EOF

djsync is installed.

  Start it:  double-click "djsync" on your Desktop
             (the first time, macOS may ask: right-click it, choose Open, then Open)
  Update:    run this installer again

The first time, djsync opens a short setup page in your browser.
EOF
