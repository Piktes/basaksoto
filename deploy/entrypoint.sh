#!/bin/sh
# Kapsayıcı giriş noktası.
#
#   Normal mod      : sanal ekranı açar, botu çalıştırır; bot kapanırsa (/restart,
#                     çökme) yeniden başlatır.
#   LOGIN_MODE=true : botu başlatmaz; kalıcı profille YouTube Studio'yu açar ki
#                     noVNC üzerinden Google'a bir kez elle giriş yapılabilsin.
#   ENABLE_VNC=true : sanal ekranı 6080 portundan (noVNC) yayınlar. VNC_PASSWORD şart.
set -eu

cd /app
mkdir -p credentials data/images data/temp data/browser_profile logs

# Google kimlik dosyaları ortam değişkeninden (base64) bir kez yazılır.
# Var olan dosyanın üzerine yazılmaz: bot token.json'u kendisi yeniler.
write_b64() { # $1 = değer, $2 = hedef dosya
  if [ -n "$1" ] && [ ! -s "$2" ]; then
    printf '%s' "$1" | base64 -d > "$2"
    chmod 600 "$2"
    echo "[entrypoint] $2 ortam değişkeninden yazıldı."
  fi
}
write_b64 "${GOOGLE_CLIENT_SECRET_B64:-}" credentials/client_secret.json
write_b64 "${GOOGLE_TOKEN_B64:-}" credentials/token.json

# Sanal ekran
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1600x1000x24 -nolisten tcp >/dev/null 2>&1 &
sleep 1
fluxbox >/dev/null 2>&1 &

if [ "${ENABLE_VNC:-false}" = "true" ]; then
  if [ -z "${VNC_PASSWORD:-}" ]; then
    echo "[entrypoint] HATA: ENABLE_VNC=true ama VNC_PASSWORD boş. Parolasız VNC açılmaz." >&2
    exit 1
  fi
  x11vnc -display :99 -forever -shared -localhost -passwd "$VNC_PASSWORD" -quiet -bg >/dev/null 2>&1
  websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
  echo "[entrypoint] noVNC açık (6080): https://ALAN-ADI/vnc.html"
fi

if [ "${LOGIN_MODE:-false}" = "true" ]; then
  echo "[entrypoint] GİRİŞ MODU: bot çalışmıyor. noVNC'den Google'a giriş yapın,"
  echo "[entrypoint] sonra LOGIN_MODE=false yapıp yeniden dağıtın."
  exec python -m deploy.login
fi

# Bot döngüsü: /restart ve /guncelle botu kapatır; burada yeniden başlar.
while true; do
  python -m bot || echo "[entrypoint] Bot kapandı (çıkış kodu $?)."
  sleep 5
done
