# Container for the b-roll editor. Includes ffmpeg (required for rendering).
FROM python:3.12-slim

# ffmpeg is the core dependency — Vercel/serverless can't provide it, a container can.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Hosts (Railway/Render/Fly) inject $PORT; default to 8000 locally.
ENV PORT=8000
EXPOSE 8000

# gunicorn serves the Flask app. One worker keeps the in-memory upload map + a
# long timeout so full-video renders don't get cut off. A multi-minute 4K HDR
# source takes several minutes to tone-map and encode, and the old 300s timeout
# killed the worker mid-render — which looked like a hang, not an error.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 4 --timeout 3600 app:app"]
