import os
import re
import glob
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp


# ─── Startup: install nightly yt-dlp + Deno ────────────────────────────────

def install_nightly_ytdlp():
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "--pre", "yt-dlp[default]"],
            timeout=120, capture_output=True
        )
        print("✅ yt-dlp nightly updated")
    except Exception as e:
        print(f"⚠️ yt-dlp update failed: {e}")


def install_deno():
    deno_path = "/usr/local/bin/deno"
    if os.path.exists(deno_path):
        print("✅ Deno already installed")
        return
    try:
        subprocess.run(
            "curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh",
            shell=True, timeout=120, capture_output=True
        )
        if os.path.exists(deno_path):
            print("✅ Deno installed successfully")
        else:
            print("⚠️ Deno install ran but binary not found")
    except Exception as e:
        print(f"⚠️ Deno install failed: {e}")


def copy_cookies():
    src = "/etc/secrets/cookies.txt"
    dst = "/tmp/cookies.txt"
    if os.path.exists(src) and not os.path.exists(dst):
        try:
            shutil.copy2(src, dst)
            print("✅ Cookies copied to /tmp/cookies.txt")
        except Exception as e:
            print(f"⚠️ Cookie copy failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    install_nightly_ytdlp()
    install_deno()
    copy_cookies()
    yield


# ─── App ────────────────────────────────────────────────────────────────────

app = FastAPI(title="SHEIKH Downloader API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Models ─────────────────────────────────────────────────────────────────

class VideoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str


# ─── Helpers ────────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name.strip()[:80]


def get_format_label(fmt: dict) -> str:
    height = fmt.get("height")
    ext = fmt.get("ext", "?")
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    size_str = f" (~{size / 1024 / 1024:.1f} MB)" if size else ""
    if height:
        return f"{height}p ({ext}){size_str}"
    return f"Audio only ({ext}){size_str}"


def get_base_opts() -> dict:
    deno_path = "/usr/local/bin/deno"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["web_embedded", "web", "android"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "socket_timeout": 45,
        "retries": 5,
        "fragment_retries": 5,
        "nocheckcertificate": True,
    }

    # Add Deno JS runtime if available
    if os.path.exists(deno_path):
        opts["js_runtimes"] = [f"deno:{deno_path}"]

    # Add cookies if available
    for cookie_path in ["/tmp/cookies.txt", "/etc/secrets/cookies.txt"]:
        if os.path.exists(cookie_path):
            opts["cookiefile"] = cookie_path
            break

    return opts


# ─── Routes ─────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "SHEIKH Downloader is running ✅"}


@app.get("/check")
def check():
    import importlib.metadata
    deno_path = "/usr/local/bin/deno"
    try:
        ver = importlib.metadata.version("yt-dlp")
    except Exception:
        ver = "unknown"

    return {
        "yt_dlp_version": ver,
        "deno_found": os.path.exists(deno_path),
        "secret_cookies": os.path.exists("/etc/secrets/cookies.txt"),
        "tmp_cookies": os.path.exists("/tmp/cookies.txt"),
    }


@app.post("/info")
def get_video_info(request: VideoRequest):
    try:
        opts = get_base_opts()
        opts["skip_download"] = True

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(request.url, download=False)

        title = info.get("title", "Unknown")
        thumbnail = info.get("thumbnail") or "https://via.placeholder.com/200x120?text=No+Thumbnail"
        duration = info.get("duration", 0)
        uploader = info.get("uploader", "Unknown")

        formats = []
        seen_heights = set()

        # Best quality
        formats.append({
            "format_id": "bestvideo+bestaudio/best",
            "label": "🏆 Best Quality (Auto)",
            "ext": "mp4"
        })

        # Specific resolutions
        for fmt in sorted(info.get("formats", []), key=lambda x: x.get("height") or 0, reverse=True):
            height = fmt.get("height")
            if not height or height in seen_heights:
                continue
            if fmt.get("vcodec", "none") == "none":
                continue
            seen_heights.add(height)
            formats.append({
                "format_id": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
                "label": get_format_label(fmt),
                "ext": "mp4"
            })
            if len(seen_heights) >= 5:
                break

        # Audio only
        formats.append({
            "format_id": "bestaudio[ext=m4a]/bestaudio/best",
            "label": "🎵 Audio Only (MP3)",
            "ext": "mp3"
        })

        return {
            "title": title,
            "thumbnail": thumbnail,
            "duration": duration,
            "uploader": uploader,
            "formats": formats
        }

    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.post("/download")
def download_video(request: DownloadRequest):
    try:
        # Clean old temp files
        for f in glob.glob("/tmp/sheikh_dl_temp.*"):
            try:
                os.remove(f)
            except Exception:
                pass

        is_audio = request.format_id.startswith("bestaudio") or "audio" in request.format_id.lower()

        opts = get_base_opts()
        opts.update({
            "format": request.format_id,
            "outtmpl": "/tmp/sheikh_dl_temp.%(ext)s",
            "merge_output_format": "mp4",
        })

        if is_audio:
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(request.url, download=True)
            title = sanitize_filename(info.get("title", "video"))

        # Find the downloaded file
        files = glob.glob("/tmp/sheikh_dl_temp.*")
        if not files:
            return JSONResponse(status_code=500, content={"error": "File not found after download"})

        file_path = files[0]
        ext = os.path.splitext(file_path)[1].lstrip(".")
        media_type = "audio/mpeg" if ext == "mp3" else "video/mp4"
        filename = f"{title}.{ext}"

        def file_stream():
            try:
                with open(file_path, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        yield chunk
            finally:
                try:
                    os.remove(file_path)
                except Exception:
                    pass

        return StreamingResponse(
            file_stream(),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
