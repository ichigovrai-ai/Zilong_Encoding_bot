FROM python:3.12-slim-bookworm

# FFmpeg with libass (Debian's build includes libass, libx264, libx265,
# libsvtav1, libopus and aac) + fonts for subtitle fallback rendering.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg mediainfo fonts-dejavu-core fonts-noto-cjk aria2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ bot/
COPY pyproject.toml .

# Data (SQLite + session) and work dirs live on a volume.
RUN mkdir -p /app/data /app/work /app/downloads
VOLUME ["/app/data", "/app/work"]

ENV PYTHONUNBUFFERED=1

# Simple liveness check: the process keeps a fresh mtime on its log file.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD python -c "import os,sys,time; p='bot.log'; \
sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 900 else 1)"

# python -m bot handles SIGTERM via pyrogram idle() → graceful shutdown.
STOPSIGNAL SIGTERM
CMD ["python", "-m", "bot"]
