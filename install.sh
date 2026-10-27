#!/usr/bin/env bash
# JJ-TTS installer for Arch / CachyOS
# Usage: ./install.sh [--src=FILE] [--skip-models] [--skip-stt] [--uninstall]
# Put this file next to jj-tts-test.py (or jj-tts.py) and run it as your normal user (not root).
set -u

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/jj-tts"
VENV="$HOME/.venvs/jj-tts"
BIN="$HOME/.local/bin"
KOKORO_DIR="$HOME/.local/share/kokoro"
PIPER_DIR="$HOME/.local/share/piper-voices"
DESKTOP="$HOME/.local/share/applications/dev.jjtts.App.desktop"

KOKORO_BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
PIPER_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"

SRC_FILE=""   # auto-detected below (jj-tts-test.py first, then jj-tts.py), or set with --src=FILE
SKIP_MODELS=0
SKIP_STT=0
FAILED=()

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

for arg in "$@"; do
  case "$arg" in
    --skip-models) SKIP_MODELS=1 ;;
    --skip-stt)    SKIP_STT=1 ;;
    --uninstall)
      say "Uninstalling JJ-TTS (voice models are kept)"
      pkill -f jj-tts.py 2>/dev/null
      rm -rf "$APP_DIR" "$VENV" "$BIN/jj-tts" "$DESKTOP"
      echo "Done. Config is still in ~/.config/jj-tts, models in $KOKORO_DIR and $PIPER_DIR."
      exit 0 ;;
    --src=*) SRC_FILE="${arg#--src=}" ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) warn "Unknown option: $arg" ;;
  esac
done

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user, not root (it will ask for sudo when needed)."; exit 1
fi
if [ -z "$SRC_FILE" ]; then
  for f in jj-tts-test.py jj-tts.py; do
    [ -f "$SRC_DIR/$f" ] && { SRC_FILE="$SRC_DIR/$f"; break; }
  done
fi
case "$SRC_FILE" in /*|"") ;; *) SRC_FILE="$SRC_DIR/$SRC_FILE" ;; esac
if [ -z "$SRC_FILE" ] || [ ! -f "$SRC_FILE" ]; then
  echo "App file not found. Put jj-tts-test.py (or jj-tts.py) next to install.sh, or use --src=FILE"; exit 1
fi
echo "Using app file: $SRC_FILE"

# 0. Clean up the old "mutetts" install if present
if [ -e "$HOME/.local/bin/mutetts" ] || [ -d "$HOME/.venvs/mutetts" ]; then
  say "Removing old MuteTTS install"
  pkill -f mutetts.py 2>/dev/null
  rm -rf "$HOME/.local/share/mutetts" "$HOME/.venvs/mutetts" "$HOME/.local/bin/mutetts" \
         "$HOME/.local/share/applications/mutetts.desktop"
fi

# 1. System packages ---------------------------------------------------------
say "Installing system packages (needs sudo)"
sudo pacman -S --needed --noconfirm python python-pip python-gobject gtk4 libadwaita \
  libpulse ffmpeg python-soundfile curl || { echo "pacman failed, aborting."; exit 1; }

# 2. Python venv -------------------------------------------------------------
say "Creating Python venv at $VENV"
python -m venv --system-site-packages "$VENV" || { echo "venv creation failed"; exit 1; }
PIP="$VENV/bin/pip"
"$PIP" install --upgrade pip >/dev/null 2>&1

pip_try() {  # pip_try <label> <packages...>
  local label="$1"; shift
  say "Installing $label"
  if ! "$PIP" install "$@"; then
    warn "$label failed to install"
    FAILED+=("$label")
  fi
}

pip_try "gTTS" gTTS
pip_try "Kokoro (kokoro-onnx)" kokoro-onnx
pip_try "Piper (piper-tts)" piper-tts
[ "$SKIP_STT" -eq 0 ] && pip_try "Speech-to-text (faster-whisper)" faster-whisper

# 3. App files + launcher ----------------------------------------------------
say "Installing app"
mkdir -p "$APP_DIR" "$BIN" "$(dirname "$DESKTOP")"
for img in sidebar.png sidebar.webp sidebar.jpg; do
  [ -f "$SRC_DIR/$img" ] && cp "$SRC_DIR/$img" "$APP_DIR/$img" && echo "Installed sidebar image: $img"
done
cp "$SRC_FILE" "$APP_DIR/jj-tts.py"   # always installed under the stable name jj-tts.py

cat > "$BIN/jj-tts" <<EOF
#!/bin/sh
# Launcher: runs JJ-TTS inside its venv (so piper and the TTS modules are found)
export PATH="$VENV/bin:\$PATH"
exec "$VENV/bin/python" "$APP_DIR/jj-tts.py" "\$@"
EOF
chmod +x "$BIN/jj-tts"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=JJ-TTS
Comment=Text-to-speech for mute gamers
StartupWMClass=dev.jjtts.App
Exec=$BIN/jj-tts
Icon=audio-input-microphone
Categories=AudioVideo;Audio;Game;
Terminal=false
EOF

# 4. Models ------------------------------------------------------------------
fetch() {  # fetch <url> <dest>
  if [ -s "$2" ]; then echo "already have $(basename "$2")"; return 0; fi
  curl -L --fail --progress-bar -o "$2.part" "$1" && mv "$2.part" "$2" || {
    rm -f "$2.part"; warn "download failed: $1"; FAILED+=("download $(basename "$2")"); }
}

if [ "$SKIP_MODELS" -eq 0 ]; then
  say "Downloading Kokoro model (~340 MB)"
  mkdir -p "$KOKORO_DIR"
  fetch "$KOKORO_BASE/kokoro-v1.0.onnx" "$KOKORO_DIR/kokoro-v1.0.onnx"
  fetch "$KOKORO_BASE/voices-v1.0.bin"  "$KOKORO_DIR/voices-v1.0.bin"

  say "Downloading a starter Piper voice (en_US lessac medium)"
  mkdir -p "$PIPER_DIR"
  fetch "$PIPER_BASE/en_US-lessac-medium.onnx"      "$PIPER_DIR/en_US-lessac-medium.onnx"
  fetch "$PIPER_BASE/en_US-lessac-medium.onnx.json" "$PIPER_DIR/en_US-lessac-medium.onnx.json"
fi

# 5. Sanity check ------------------------------------------------------------
say "Checking installation"
check() { "$VENV/bin/python" -c "import $1" 2>/dev/null && echo "  ok   $2" || { echo "  MISSING $2"; FAILED+=("$2 import"); }; }
check gi "GTK bindings (gi)"
check gtts "gTTS"
check kokoro_onnx "kokoro-onnx"
check soundfile "soundfile"
[ "$SKIP_STT" -eq 0 ] && check faster_whisper "faster-whisper"
[ -x "$VENV/bin/piper" ] && echo "  ok   piper" || { echo "  MISSING piper"; FAILED+=("piper command"); }

echo
case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN is not in your PATH. Add it, or run $BIN/jj-tts directly." ;; esac

if [ "${#FAILED[@]}" -gt 0 ]; then
  warn "Finished with problems: ${FAILED[*]}"
  echo "JJ-TTS still runs; engines that failed just won't work. Re-run ./install.sh to retry."
else
  say "All done! Run: jj-tts"
fi

cat <<EOF

Next steps:
  * Start the app:            jj-tts
  * Bind a desktop shortcut:  jj-tts --quick
  * Virtual mic for games:    Settings -> "Create virtual mic"
EOF