#!/usr/bin/env bash
# AutoEventPlay installer for Raspberry Pi OS (Bookworm or newer, 64-bit recommended).
#
#   git clone <repo> auto-event-play && cd auto-event-play
#   sudo ./deploy/install.sh
#
# Re-running the script updates an existing installation (data is kept).
set -euo pipefail

APP_DIR=/opt/autoeventplay
DATA_DIR=/var/lib/autoeventplay
ENV_FILE=/etc/autoeventplay.env
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VOICES=(
  "de/de_DE/thorsten/high/de_DE-thorsten-high"
  "en/en_GB/alan/medium/en_GB-alan-medium"
)
VOICE_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"

if [[ $EUID -ne 0 ]]; then
  echo "Bitte mit sudo ausführen." >&2
  exit 1
fi

echo "==> Systempakete"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-dev libportaudio2 ffmpeg espeak-ng curl alsa-utils

echo "==> Benutzer 'aep'"
if ! id aep >/dev/null 2>&1; then
  useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin --groups audio aep
fi
usermod -aG audio aep

echo "==> Programm nach $APP_DIR"
mkdir -p "$APP_DIR" "$DATA_DIR/voices"
rsync -a --delete --exclude ".git" --exclude ".venv" --exclude "data" --exclude "__pycache__" "$SRC_DIR/" "$APP_DIR/" 2>/dev/null \
  || { rm -rf "$APP_DIR/autoeventplay"; cp -r "$SRC_DIR/." "$APP_DIR/"; }

echo "==> Python-Umgebung"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
if ! "$APP_DIR/.venv/bin/pip" install -q piper-tts; then
  echo "    Hinweis: piper-tts konnte nicht installiert werden – espeak-ng wird als Sprachausgabe verwendet."
fi

echo "==> Bootstrap (offline verfügbar)"
bash "$APP_DIR/scripts/fetch-vendor.sh" || echo "    Hinweis: Bootstrap konnte nicht geladen werden (Weboberfläche lädt es dann aus dem Internet)."

echo "==> Piper-Stimmen"
for v in "${VOICES[@]}"; do
  name="$(basename "$v")"
  if [[ ! -f "$DATA_DIR/voices/$name.onnx" ]]; then
    curl -fsSL "$VOICE_BASE/$v.onnx" -o "$DATA_DIR/voices/$name.onnx" && \
    curl -fsSL "$VOICE_BASE/$v.onnx.json" -o "$DATA_DIR/voices/$name.onnx.json" && echo "    $name" \
      || echo "    Hinweis: Stimme $name konnte nicht geladen werden."
  fi
done

echo "==> Konfiguration"
[[ -f "$ENV_FILE" ]] || cp "$APP_DIR/deploy/autoeventplay.env" "$ENV_FILE"
chown -R aep:aep "$DATA_DIR"
chown -R root:root "$APP_DIR"

echo "==> Dienst"
cp "$APP_DIR/deploy/autoeventplay.service" /etc/systemd/system/autoeventplay.service
systemctl daemon-reload
systemctl enable --now autoeventplay.service
systemctl restart autoeventplay.service

PORT="$(grep -E '^AEP_PORT=' "$ENV_FILE" | cut -d= -f2)"
echo
echo "Fertig. Weboberfläche: http://$(hostname -I | awk '{print $1}'):${PORT:-8080}/"
echo "Beim ersten Aufruf wird das Administrator-Konto angelegt."
