"""
Video Ingestion Microservice for the Creator Content OS.

POST /ingest  {"url": "<video url>"}  -> metadata + thumbnail + caption + transcript
GET  /trends?count=20                -> trending TikTok videos (degrades gracefully)

No API keys required for the core path. URLs are never logged or stored.
"""

import asyncio
import os
import re
import shutil
import tempfile
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

import yt_dlp
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Video Ingestion Service", version="1.0.0")

# Optional Netscape-format cookies file for sites (notably Instagram) that
# require a logged-in session. Set YTDLP_COOKIES=/path/to/cookies.txt
COOKIES_FILE = os.environ.get("YTDLP_COOKIES")
# Optional TikTok ms_token cookie value to improve trend-fetch reliability.
TIKTOK_MS_TOKEN = os.environ.get("TIKTOK_MS_TOKEN")

TRANSCRIPT_MAX_CHARS = 20000
CAPTION_MAX_CHARS = 2000


class IngestRequest(BaseModel):
    url: str


# ---------------------------------------------------------------- platform

def detect_platform(url: str) -> str:
    u = url.lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u:
        return "instagram"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    return "other"


def ydl_opts_base(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    # YouTube increasingly requires a JS runtime to resolve media URLs
    # (needed for the audio-download fallback). Prefer node when present.
    if shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    if extra:
        opts.update(extra)
    return opts


def friendly_error(exc: Exception, platform: str) -> str:
    msg = str(exc).lower()
    if "unsupported url" in msg:
        return "That link isn't a supported video page. Paste a direct TikTok, Instagram, or YouTube video link."
    if "private" in msg:
        return "That video is private, so its details can't be read."
    if "login required" in msg or "not logged in" in msg:
        if platform == "instagram":
            return (
                "Instagram asked for a login to read that video. "
                "Provide a cookies.txt file via the YTDLP_COOKIES setting and try again."
            )
        return "That site asked for a login to read the video."
    if "not available" in msg or "removed" in msg or "deleted" in msg:
        return "That video is unavailable — it may have been removed."
    if "timed out" in msg or "timeout" in msg:
        return "The video site took too long to respond. Try again in a moment."
    return "Couldn't read that video. Double-check the link and try again."


# ---------------------------------------------------------------- metadata

def extract_metadata(url: str) -> Dict[str, Any]:
    with yt_dlp.YoutubeDL(ydl_opts_base()) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as exc:  # yt_dlp raises DownloadError subclasses
            raise ValueError(str(exc)) from exc
    if not info:
        raise ValueError("no info returned")
    return info


def iso_date(upload_date: Optional[str]) -> Optional[str]:
    # yt-dlp upload_date is YYYYMMDD
    if not upload_date or len(upload_date) != 8 or not upload_date.isdigit():
        return None
    try:
        return datetime.strptime(upload_date, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------- captions

def pick_caption_track_url(info: Dict[str, Any]) -> Optional[str]:
    """Prefer manually uploaded subtitles, fall back to auto-generated."""
    for key in ("subtitles", "automatic_captions"):
        tracks = info.get(key) or {}
        if not tracks:
            continue
        for lang in ("en", "en-US", "en-GB", "en-orig"):
            if lang in tracks and tracks[lang]:
                url = tracks[lang][0].get("url")
                if url:
                    return url
        for _lang, arr in tracks.items():
            if arr and arr[0].get("url"):
                return arr[0]["url"]
    return None


def fetch_url_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean_subtitle_text(raw: str) -> str:
    """Strip WebVTT/SRT markup down to plain spoken text."""
    lines: List[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if "-->" in line:
            continue
        if re.match(r"^\d+$", line):  # SRT cue index
            continue
        line = re.sub(r"<[^>]+>", "", line)  # <c>, <i>, timestamps in tags
        line = re.sub(r"\d{2}:\d{2}:\d{2}[.,]\d{3}", "", line).strip()
        line = re.sub(r"\s{2,}", " ", line).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return " ".join(lines)


def transcript_from_captions(info: Dict[str, Any]) -> str:
    track_url = pick_caption_track_url(info)
    if not track_url:
        return ""
    try:
        raw = fetch_url_text(track_url)
    except Exception:
        return ""
    return clean_subtitle_text(raw)


# ---------------------------------------------------------------- whisper

_whisper_model = None


def _load_whisper_model():
    from faster_whisper import WhisperModel

    # small model, int8 CPU: light, no torch needed
    return WhisperModel("small", device="cpu", compute_type="int8")


def _proxy_env_keys():
    # Any *proxy* var (incl. no_proxy/NO_PROXY: some sandboxes set values
    # like *[::1] that break httpx's proxy-URL parsing outright).
    return [k for k in os.environ if "proxy" in k.lower()]


def get_whisper_model():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    try:
        _whisper_model = _load_whisper_model()
        return _whisper_model
    except Exception as first_error:
        # Some sandboxed/proxied environments break huggingface_hub's HTTP
        # client. If the model is already in the local cache, retry fully
        # offline with proxy config stripped.
        saved_proxies = {k: os.environ.pop(k) for k in _proxy_env_keys()}
        os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            _whisper_model = _load_whisper_model()
            return _whisper_model
        except Exception:
            raise first_error
        finally:
            os.environ.update(saved_proxies)
            os.environ.pop("HF_HUB_OFFLINE", None)


def download_audio_wav(url: str) -> str:
    """Extract best audio to a temp wav file. Returns (wav_path, tmpdir)."""
    tmpdir = tempfile.mkdtemp(prefix="ingest_audio_")
    outtmpl = os.path.join(tmpdir, "audio.%(ext)s")
    opts = ydl_opts_base(
        {
            "skip_download": False,
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "wav"}
            ],
        }
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise ValueError(str(exc)) from exc
    wav_path = os.path.join(tmpdir, "audio.wav")
    if not os.path.exists(wav_path):
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise ValueError("audio extraction produced no file")
    return wav_path, tmpdir


def transcript_from_whisper(url: str) -> str:
    wav_path, tmpdir = download_audio_wav(url)
    try:
        model = get_whisper_model()
        segments, _ = model.transcribe(wav_path, beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
        return text
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- endpoints

@app.get("/health")
def health():
    return {"ok": True}


@app.post("/ingest")
def ingest(req: IngestRequest):
    url = (req.url or "").strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse(
            status_code=422,
            content={"error": "That doesn't look like a web link. Paste the full video URL starting with https://."},
        )

    platform = detect_platform(url)

    try:
        info = extract_metadata(url)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": friendly_error(exc, platform)})

    title = info.get("title") or "Untitled video"
    creator = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
    caption = (info.get("description") or "")[:CAPTION_MAX_CHARS]
    thumbnail_url = info.get("thumbnail") or ""
    duration = info.get("duration") or 0
    view_count = info.get("view_count") or 0
    published_at = iso_date(info.get("upload_date"))

    # Transcript: native captions first (free), Whisper fallback.
    transcript = transcript_from_captions(info)
    transcript_source = "captions"
    if not transcript:
        try:
            transcript = transcript_from_whisper(url)
            transcript_source = "whisper"
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content={"error": friendly_error(exc, platform) + " (no captions were available to read instead)"},
            )
        except Exception:
            return JSONResponse(
                status_code=422,
                content={"error": "The video was found, but no transcript could be produced from it."},
            )

    transcript = transcript[:TRANSCRIPT_MAX_CHARS]

    return {
        "title": title,
        "creator": creator,
        "caption": caption,
        "thumbnail_url": thumbnail_url,
        "transcript": transcript,
        "transcript_source": transcript_source,
        "duration_sec": duration,
        "view_count": view_count,
        "platform": platform,
        "published_at": published_at,
        "source_url": url,
    }


async def _fetch_trending(count: int) -> List[Dict[str, Any]]:
    from TikTokApi import TikTokApi  # lazy: trends are best-effort

    videos: List[Dict[str, Any]] = []
    async with TikTokApi() as api:
        ms_tokens = [TIKTOK_MS_TOKEN] if TIKTOK_MS_TOKEN else [None]
        await api.create_sessions(ms_tokens=ms_tokens, num_sessions=1, sleep_after=3)
        async for video in api.trending.videos(count=count):
            d = video.as_dict or {}
            author = d.get("author") or {}
            v = d.get("video") or {}
            stats = d.get("stats") or {}
            desc = d.get("desc") or ""
            vid_id = d.get("id")
            handle = author.get("uniqueId") or ""
            source_url = f"https://www.tiktok.com/@{handle}/video/{vid_id}" if vid_id else ""
            videos.append(
                {
                    "title": desc[:200] if desc else f"Video by @{handle}" if handle else "Trending video",
                    "creator": handle,
                    "caption": desc,
                    "thumbnail_url": v.get("cover") or "",
                    "view_count": stats.get("playCount") or 0,
                    "platform": "tiktok",
                    "source_url": source_url,
                }
            )
            if len(videos) >= count:
                break
    return videos


@app.get("/trends")
def trends(count: int = 20):
    """Trending TikTok videos. Never 500s: partial results + an errors note."""
    count = max(1, min(count, 50))
    try:
        videos = asyncio.run(_fetch_trending(count))
    except ImportError:
        return {
            "videos": [],
            "errors": "Trend discovery isn't installed on this server (TikTokApi missing).",
        }
    except Exception as exc:
        return {
            "videos": [],
            "errors": (
                "Couldn't reach TikTok's trending feed right now "
                "(it actively blocks automated access). Try again later."
            ),
        }
    if not videos:
        return {"videos": [], "errors": "TikTok returned no trending videos this time."}
    return {"videos": videos}
