FROM python:3.12-slim

# ffmpeg is required for audio extraction (Whisper fallback path)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Optional: bake in Instagram cookies (see README). Prefer passing at runtime.
# COPY cookies.txt /srv/cookies.txt
# ENV YTDLP_COOKIES=/srv/cookies.txt

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
