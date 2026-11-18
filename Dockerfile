# Basaksoto botu — sunucu (Docker / Dokploy) imajı.
# Tarayıcı "headful" çalışır: sanal ekran (Xvfb) üzerinde açılır, gerektiğinde
# noVNC ile tarayıcıdan izlenir (Google girişi için). Ayrıntı: deploy/entrypoint.sh
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Istanbul \
    DISPLAY=:99

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg xvfb x11vnc novnc websockify fluxbox \
        tzdata tini fonts-liberation fonts-noto-core libatomic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY bot ./bot
COPY deploy ./deploy
RUN chmod +x deploy/entrypoint.sh

EXPOSE 6080
ENTRYPOINT ["tini", "-g", "--", "/app/deploy/entrypoint.sh"]
