# Video Ingestion Service

Open-source microservice that turns a TikTok / Instagram / YouTube link into
structured data for the Creator Content OS: title, creator, caption, thumbnail,
transcript, duration, view count, platform, and publish date. Also serves a
best-effort TikTok trending feed.

No API keys are required for the core path. URLs are never logged or stored.

## How it works

- **Metadata / thumbnails / captions** — [yt-dlp](https://github.com/yt-dlp/yt-dlp)
  (no full download needed; prefers uploaded subtitles, falls back to auto-generated).
- **Transcript fallback** — if a video has no captions, audio is extracted and
  transcribed locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  (`small` model, CPU int8 — no torch, no API key).
- **Trends** — unofficial [TikTokApi](https://github.com/davidteather/TikTok-Api)
  `trending.videos()`. TikTok actively blocks scrapers, so `/trends` degrades
  gracefully (partial results + an `errors` note, never a 500).

## Run locally

```bash
cd ~/workspace/ingestion-service
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

The first Whisper transcription downloads the `small` model (~460 MB) once and
caches it in `~/.cache/huggingface`.

Test it:

```bash
python3 test.py   # exercises /ingest (YouTube + TikTok) and /trends
```

> **Sandbox note (this dev VM only):** outbound traffic goes through an
> intercepting proxy whose CA isn't in certifi's bundle, which yt-dlp uses
> directly. For local testing here, the proxy CA was appended to the venv's
> certifi bundle:
> `cat /run/hatch/egress-tls/ca-bundle.pem >> .venv/lib/python3.12/site-packages/certifi/cacert.pem`
> You don't need this on Railway/Render/Fly or any normal machine. The
> Whisper `small` model was also pre-downloaded into `~/.cache/huggingface`
> here because the proxy breaks huggingface_hub's client; the service has an
> automatic offline-cache fallback for such environments.

## API

### `POST /ingest`

Body: `{"url": "https://www.tiktok.com/@user/video/123..."}`

Success (200):

```json
{
  "title": "...",
  "creator": "...",
  "caption": "...",
  "thumbnail_url": "https://...",
  "transcript": "...",
  "transcript_source": "captions | whisper",
  "duration_sec": 42,
  "view_count": 1200000,
  "platform": "tiktok | instagram | youtube | other",
  "published_at": "2026-03-14",
  "source_url": "https://..."
}
```

Failure (422): `{"error": "<plain-language reason>"}`

### `GET /trends?count=20`

```json
{ "videos": [ {"title","creator","caption","thumbnail_url","view_count","platform":"tiktok","source_url"} ] }
```

On partial/total failure: `{"videos": [...whatever worked...], "errors": "<note>"}`

### `GET /health` → `{"ok": true}`

## Environment variables

| Var | Purpose |
| --- | ------- |
| `YTDLP_COOKIES` | Path to a Netscape-format `cookies.txt`. Needed for Instagram, which usually requires a logged-in session. Export cookies from your browser (e.g. with a "Get cookies.txt" extension) while logged into instagram.com, then `YTDLP_COOKIES=/path/to/cookies.txt uvicorn app:app`. |
| `TIKTOK_MS_TOKEN` | Optional `ms_token` cookie value from tiktok.com — improves `/trends` reliability. |
| `PORT` | Port to listen on (default 8000). |

## Deploy

### Railway

```bash
railway init        # or link an existing project
railway up          # uses the Dockerfile automatically
railway variables set YTDLP_COOKIES=/srv/cookies.txt   # if needed
```

### Render

New → Web Service → point at this directory (Docker runtime). Set env vars in
the dashboard. Note: the free tier sleeps; the first Whisper transcription
after a cold start downloads the model.

### Fly.io

```bash
fly launch          # accepts the Dockerfile
fly secrets set TIKTOK_MS_TOKEN=...   # optional
fly deploy
```

Give the machine at least 1 GB RAM (Whisper `small` int8 uses ~1 GB at peak).

## Connecting the Creator Content OS

Point the app's "Save video" action at `POST {SERVICE_URL}/ingest` with
`{"url": "<pasted link>"}` and store the returned fields as the video record.
Point the discovery feed at `GET {SERVICE_URL}/trends?count=20`.
