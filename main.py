import os
import sys
import subprocess
import shutil
import platform
import zipfile
import urllib.request
import asyncio
import tempfile
import glob
import re
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel


# ─────────────────────────────────────────
# PIPED API INSTANCES (fallback list)
# ─────────────────────────────────────────

PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.yt",
    "https://pipedapi.leptons.xyz",
    "https://piped-api.privacy.com.de",
    "https://pipedapi.reallyaweso.me",
    "https://pipedapi.ducks.party",
    "https://api.piped.private.coffee",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


# ─────────────────────────────────────────
# STARTUP HELPERS
# ─────────────────────────────────────────

def run_cmd(cmd: list, timeout: int = 180) -> tuple:
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout,
        )
        return result.returncode, result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except Exception as e:
        return -1, "", str(e)


def update_ytdlp():
    print("⏳ Updating yt-dlp nightly (fallback engine)...")
    code, _, err = run_cmd([
        sys.executable, "-m", "pip", "install", "-U", "--pre",
        "yt-dlp[default]"
    ], timeout=180)
    if code == 0:
        print("✅ yt-dlp nightly updated")
    else:
        print(f"⚠️  yt-dlp update warning: {err[-200:]}")


def install_deno():
    deno_path = "/tmp/deno"
    if os.path.exists(deno_path):
        os.environ["PATH"] = "/tmp:" + os.environ.get("PATH", "")
        print("✅ Deno already installed")
        return
    print("⏳ Installing Deno...")
    arch = platform.machine().lower()
    url = (
        "https://github.com/denoland/deno/releases/latest/download/deno-aarch64-unknown-linux-gnu.zip"
        if "aarch64" in arch or "arm64" in arch
        else "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"
    )
    try:
        urllib.request.urlretrieve(url, "/tmp/deno_dl.zip")
        with zipfile.ZipFile("/tmp/deno_dl.zip", "r") as z:
            z.extractall("/tmp")
        os.chmod("/tmp/deno", 0o755)
        os.environ["PATH"] = "/tmp:" + os.environ.get("PATH", "")
        print("✅ Deno installed at /tmp/deno")
    except Exception as e:
        print(f"⚠️  Deno install failed: {e}")


def copy_cookies():
    src, dst = "/etc/secrets/cookies.txt", "/tmp/cookies.txt"
    if os.path.exists(src) and not os.path.exists(dst):
        shutil.copy2(src, dst)
        print("✅ Cookies copied")
    elif os.path.exists(dst):
        print("✅ Cookies at /tmp/cookies.txt")
    else:
        print("ℹ️  No cookies file (optional)")


def get_cookie_file():
    for p in ["/tmp/cookies.txt", "/etc/secrets/cookies.txt"]:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def extract_video_id(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|embed/|shorts/)([a-zA-Z0-9_-]{11})", url)
    return m.group(1) if m else None


# ─────────────────────────────────────────
# PIPED API (PRIMARY — 100% FREE)
# ─────────────────────────────────────────

def piped_get_streams(video_id: str) -> dict | None:
    """Try multiple Piped instances until one works."""
    for instance in PIPED_INSTANCES:
        try:
            r = requests.get(
                f"{instance}/streams/{video_id}",
                headers=HEADERS,
                timeout=20,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("videoStreams") or data.get("audioStreams"):
                    print(f"✅ Piped working: {instance}")
                    return data
        except Exception as e:
            print(f"⚠️  Piped instance {instance} failed: {e}")
            continue
    return None


def piped_download(video_id: str, quality: str = "best") -> str | None:
    """Download video via Piped stream URL. Returns file path or None."""
    data = piped_get_streams(video_id)
    if not data:
        return None

    video_streams = data.get("videoStreams", [])
    audio_streams = data.get("audioStreams", [])

    # Filter non-video-only streams (combined audio+video)
    combined = [s for s in video_streams if not s.get("videoOnly", True)]

    # If combined stream exists, use it
    if combined:
        # Sort by quality: pick best
        def get_height(s):
            return s.get("height") or 0
        combined.sort(key=get_height, reverse=True)
        stream = combined[0]
        stream_url = stream.get("url")
        ext = "mp4"
    elif video_streams:
        # video-only: pick best resolution
        video_streams.sort(key=lambda s: s.get("height") or 0, reverse=True)
        stream = video_streams[0]
        stream_url = stream.get("url")
        ext = "mp4"
    elif audio_streams:
        audio_streams.sort(key=lambda s: s.get("bitrate") or 0, reverse=True)
        stream = audio_streams[0]
        stream_url = stream.get("url")
        ext = "m4a"
    else:
        return None

    if not stream_url:
        return None

    out_path = f"/tmp/sheikh_{video_id}.{ext}"
    try:
        resp = requests.get(
            stream_url,
            stream=True,
            headers=HEADERS,
            timeout=120,
        )
        if resp.status_code == 200:
            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            return out_path
        else:
            print(f"⚠️  Piped stream download failed: HTTP {resp.status_code}")
            return None
    except Exception as e:
        print(f"⚠️  Piped download error: {e}")
        return None


# ─────────────────────────────────────────
# YT-DLP (FALLBACK)
# ─────────────────────────────────────────

def get_ytdlp_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 45,
        "retries": 5,
        "fragment_retries": 5,
        "nocheckcertificate": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["mweb", "web", "android"],
            }
        },
    }
    cookie = get_cookie_file()
    if cookie:
        opts["cookiefile"] = cookie
    return opts


# ─────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    update_ytdlp()
    install_deno()
    copy_cookies()
    yield


app = FastAPI(
    title="SHEIKH Downloader API",
    version="6.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class URLRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str = "best"
    audio_only: bool = False


# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "SHEIKH Downloader API v6.0 🚀", "engine": "Piped API (free) + yt-dlp fallback"}


@app.get("/check")
def check():
    import importlib.metadata as meta
    try:
        ytdlp_ver = meta.version("yt-dlp")
    except Exception:
        ytdlp_ver = "unknown"

    # Quick ping to first Piped instance
    piped_ok = False
    working_instance = None
    for inst in PIPED_INSTANCES[:3]:
        try:
            r = requests.get(f"{inst}/trending?region=US", timeout=5)
            if r.status_code == 200:
                piped_ok = True
                working_instance = inst
                break
        except Exception:
            continue

    return {
        "status": "ok",
        "yt_dlp_version": ytdlp_ver,
        "piped_api_working": piped_ok,
        "piped_working_instance": working_instance,
        "deno_found": os.path.exists("/tmp/deno"),
        "secret_cookies": os.path.exists("/etc/secrets/cookies.txt"),
        "tmp_cookies": os.path.exists("/tmp/cookies.txt"),
    }


@app.post("/info")
async def get_info(req: URLRequest):
    loop = asyncio.get_event_loop()
    video_id = extract_video_id(req.url)

    # ── PRIMARY: Piped API (Free, No Key) ──
    if video_id:
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, piped_get_streams, video_id),
                timeout=40,
            )
            if data:
                video_streams = data.get("videoStreams", [])
                audio_streams = data.get("audioStreams", [])
                formats = []

                for s in video_streams:
                    formats.append({
                        "format_id": f"video_{s.get('quality', 'unknown')}",
                        "ext": "mp4",
                        "resolution": s.get("quality"),
                        "height": s.get("height"),
                        "codec": s.get("codec"),
                        "bitrate": s.get("bitrate"),
                        "video_only": s.get("videoOnly", False),
                        "url": s.get("url"),
                    })
                for s in audio_streams:
                    formats.append({
                        "format_id": f"audio_{s.get('quality', 'unknown')}",
                        "ext": "m4a",
                        "resolution": s.get("quality"),
                        "codec": s.get("codec"),
                        "bitrate": s.get("bitrate"),
                        "video_only": False,
                        "url": s.get("url"),
                    })

                return {
                    "source": "piped_api",
                    "title": data.get("title", ""),
                    "thumbnail": data.get("thumbnailUrl", ""),
                    "duration": data.get("duration"),
                    "uploader": data.get("uploader", ""),
                    "views": data.get("views"),
                    "likes": data.get("likes"),
                    "description": data.get("description", "")[:500],
                    "formats": formats,
                }
        except asyncio.TimeoutError:
            print("⚠️  Piped timeout, falling back to yt-dlp")
        except Exception as e:
            print(f"⚠️  Piped info failed: {e}")

    # ── FALLBACK: yt-dlp ──
    try:
        import yt_dlp
        opts = get_ytdlp_opts()
        opts["skip_download"] = True

        def _extract():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(req.url, download=False)

        info = await asyncio.wait_for(
            loop.run_in_executor(None, _extract),
            timeout=120,
        )
        formats = []
        for f in info.get("formats", []):
            formats.append({
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution") or f.get("format_note"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
            })
        return {
            "source": "yt-dlp",
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
            "view_count": info.get("view_count"),
            "formats": formats,
        }
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out on all engines.")
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"All engines failed. Error: {str(e)}"
        )


@app.post("/download")
async def download_video(req: DownloadRequest):
    loop = asyncio.get_event_loop()
    video_id = extract_video_id(req.url)

    # ── PRIMARY: Piped API (Free) ──
    if video_id and not req.audio_only:
        try:
            filepath = await asyncio.wait_for(
                loop.run_in_executor(None, piped_download, video_id, req.format_id),
                timeout=200,
            )
            if filepath and os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                return FileResponse(
                    path=filepath,
                    filename=os.path.basename(filepath),
                    media_type="application/octet-stream",
                )
        except asyncio.TimeoutError:
            print("⚠️  Piped download timeout, trying yt-dlp")
        except Exception as e:
            print(f"⚠️  Piped download failed: {e}")

    # ── FALLBACK: yt-dlp ──
    try:
        import yt_dlp
        opts = get_ytdlp_opts()

        with tempfile.NamedTemporaryFile(
            suffix=".tmp", prefix="sheikh_", dir="/tmp", delete=False
        ) as tmp:
            base_path = tmp.name.replace(".tmp", "")

        out_tmpl = base_path + ".%(ext)s"

        if req.audio_only:
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        else:
            opts["format"] = (
                req.format_id if req.format_id not in ["best", ""]
                else "bestvideo+bestaudio/best"
            )

        opts["outtmpl"] = out_tmpl

        def _download():
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(req.url, download=True)
                return ydl.prepare_filename(info)

        filename = await asyncio.wait_for(
            loop.run_in_executor(None, _download),
            timeout=300,
        )

        if not os.path.exists(filename):
            matches = glob.glob(base_path + ".*")
            filename = matches[0] if matches else None

        if not filename or not os.path.exists(filename):
            raise HTTPException(status_code=500, detail="File not found after download")

        return FileResponse(
            path=filename,
            filename=os.path.basename(filename),
            media_type="application/octet-stream",
        )

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Download timed out.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
