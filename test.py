#!/usr/bin/env python3
"""
End-to-end test for the ingestion service.

- Starts uvicorn on port 8123
- Resolves a real public YouTube URL via yt-dlp's own search (no hard-coded IDs)
- POSTs /ingest for that YouTube URL and for a TikTok URL
- GETs /trends?count=5

Pass criteria: YouTube ingest returns 200 with a non-empty transcript,
thumbnail_url and title. TikTok is attempted but tolerated (TikTok frequently
blocks datacenter IPs). Trends must return 200 with a "videos" list
(possibly empty, with an "errors" note).

Env overrides:
  YT_TEST_URL      use this YouTube URL instead of resolving one
  TIKTOK_TEST_URL  use this TikTok URL instead of the default
  PORT             test server port (default 8123)
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8123"))
BASE = f"http://127.0.0.1:{PORT}"

# Default TikTok fixture: a real public video URL found via web search
# (override with TIKTOK_TEST_URL).
TIKTOK_TEST_URL = os.environ.get(
    "TIKTOK_TEST_URL",
    "https://www.tiktok.com/@usa.clips63/video/7629342861506481438",
)


def http_json(method, path, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "User-Agent": "ingest-test/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": "unparseable error body"}


def resolve_youtube_url():
    if os.environ.get("YT_TEST_URL"):
        return os.environ["YT_TEST_URL"]
    # Flat search is fast; then probe candidates until one is a short,
    # non-live video (livestreams have no usable duration/transcript).
    out = subprocess.run(
        [sys.executable, "-m", "yt_dlp",
         "--dump-json", "--skip-download", "--no-playlist", "--flat-playlist",
         "ytsearch8:ted talk short"],
        capture_output=True, text=True, timeout=180, cwd=SERVICE_DIR,
    )
    ids = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                ids.append(json.loads(line)["id"])
            except (KeyError, json.JSONDecodeError):
                pass
    for vid in ids:
        url = f"https://www.youtube.com/watch?v={vid}"
        try:
            probe = subprocess.run(
                [sys.executable, "-m", "yt_dlp",
                 "--dump-json", "--skip-download", "--no-playlist", url],
                capture_output=True, text=True, timeout=90, cwd=SERVICE_DIR,
            )
            for line in probe.stdout.splitlines():
                if line.strip().startswith("{"):
                    info = json.loads(line)
                    dur = info.get("duration")
                    if dur and dur < 600 and not info.get("is_live"):
                        return url
                    break
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            continue
    raise RuntimeError("could not resolve a YouTube test URL via ytsearch")


def main():
    venv_python = os.path.join(SERVICE_DIR, ".venv", "bin", "python")
    python = venv_python if os.path.exists(venv_python) else sys.executable

    print(f"[test] starting server on :{PORT} ...")
    server = subprocess.Popen(
        [python, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=SERVICE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures = []
    try:
        # wait for health
        for _ in range(60):
            try:
                status, _ = http_json("GET", "/health", timeout=5)
                if status == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            print("[test] FAIL: server did not become healthy")
            return 1
        print("[test] server healthy")

        yt_url = resolve_youtube_url()
        print(f"[test] YouTube fixture: {yt_url}")
        print("[test] POST /ingest (YouTube) ...")
        status, data = http_json("POST", "/ingest", {"url": yt_url})
        if status != 200:
            failures.append(f"YouTube ingest returned {status}: {data}")
        else:
            summary = {
                k: (len(v) if isinstance(v, str) else v)
                for k, v in data.items() if k in
                ("title", "creator", "platform", "duration_sec", "view_count",
                 "transcript_source", "published_at")
            }
            summary["caption_chars"] = len(data.get("caption", ""))
            summary["transcript_chars"] = len(data.get("transcript", ""))
            summary["thumbnail_present"] = bool(data.get("thumbnail_url"))
            print("[test] YouTube result:", json.dumps(summary, indent=2)[:1200])
            if not data.get("transcript"):
                failures.append("YouTube ingest: empty transcript")
            if not data.get("thumbnail_url"):
                failures.append("YouTube ingest: missing thumbnail_url")
            if not data.get("title"):
                failures.append("YouTube ingest: missing title")

        print(f"[test] TikTok fixture: {TIKTOK_TEST_URL}")
        print("[test] POST /ingest (TikTok) ...")
        status, data = http_json("POST", "/ingest", {"url": TIKTOK_TEST_URL})
        if status == 200 and data.get("transcript") and data.get("thumbnail_url"):
            print("[test] TikTok OK:",
                  json.dumps({"title": data.get("title"),
                              "transcript_chars": len(data.get("transcript", "")),
                              "transcript_source": data.get("transcript_source")}))
        else:
            print(f"[test] WARN: TikTok ingest did not fully verify "
                  f"(status={status}, body={str(data)[:300]}). "
                  f"TikTok often blocks datacenter IPs; YouTube verification stands.")

        print("[test] GET /trends?count=5 ...")
        status, data = http_json("GET", "/trends?count=5", timeout=180)
        if status != 200 or "videos" not in data:
            failures.append(f"/trends bad response: status={status} body={str(data)[:200]}")
        else:
            print(f"[test] /trends OK: {len(data['videos'])} videos"
                  + (f", note: {data['errors']}" if data.get("errors") else ""))

        # bad-input contract check
        status, data = http_json("POST", "/ingest", {"url": "not a url"}, timeout=30)
        if status != 422 or "error" not in data:
            failures.append("expected 422 {error} for invalid URL")
        else:
            print("[test] invalid-URL 422 contract OK")
    finally:
        server.terminate()
        server.wait(timeout=15)

    if failures:
        print("[test] FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("[test] ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
