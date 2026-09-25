from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp
import os
import re
import glob
import shutil
import subprocess
import sys

# Auto-update yt-dlp on startup
try:
    subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
                   capture_output=True, timeout=60)
except Exception:
    pass

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class VideoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name[:80].strip()

def get_format_label(f: dict) -> str:
    height = f.get("height")
    ext = f.get("ext", "mp4")
    filesize = f.get("filesize") or f.get("filesize_approx")
    size_str = f" (~{round(filesize/(1024*1024), 1)} MB)" if filesize else ""
    if height:
        return f"{height}p ({ext}){size_str}"
    return f"{ext}{size_str}"

def get_base_opts():
    cookies_src = "/etc/secrets/cookies.txt"
    cookies_dst = "/tmp/cookies.txt"

    # Copy cookies to writable /tmp folder
    if os.path.exists(cookies_src):
        try:
            shutil.copy2(cookies_src, cookies_dst)
        except Exception:
            pass

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android", "mweb"],
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "socket_timeout": 45,
        "retries": 5,
        "fragment_retries": 5,
        "nocheckcertificate": True,
    }

    if os.path.exists(cookies_dst):
        opts["cookiefile"] = cookies_dst

    return opts

@app.get("/")
def root():
    return {"status": "SHEIKH-DL Backend is running!"}

@app.get("/check-cookies")
def check_cookies():
    cookies_src = "/etc/secrets/cookies.txt"
    cookies_dst = "/tmp/cookies.txt"
    return {
        "secret_file_exists": os.path.exists(cookies_src),
        "tmp_file_exists": os.path.exists(cookies_dst),
        "secret_size": os.path.getsize(cookies_src) if os.path.exists(cookies_src) else 0,
        "tmp_size": os.path.getsize(cookies_dst) if os.path.exists(cookies_dst) else 0,
    }

@app.post("/info")
def get_video_info(req: VideoRequest):
    opts = get_base_opts()
    opts["skip_download"] = True

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=False)

        if not info:
            return JSONResponse(status_code=400, content={"error": "Could not extract video info."})

        title = info.get("title", "Unknown Title")
        thumbnail = info.get("thumbnail", "https://via.placeholder.com/200x120?text=No+Thumbnail")
        duration = info.get("duration", 0)
        uploader = info.get("uploader", "Unknown")

        formats = []
        seen_heights = set()

        formats.append({
            "format_id": "bestvideo+bestaudio/best",
            "label": "⭐ Best Quality (Auto)"
        })

        raw_formats = info.get("formats", [])
        for f in reversed(raw_formats):
            height = f.get("height")
            vcodec = f.get("vcodec", "none")
            if height and vcodec != "none" and height not in seen_heights:
                seen_heights.add(height)
                formats.append({
                    "format_id": f"{f['format_id']}+bestaudio/best",
                    "label": get_format_label(f)
                })

        formats.append({
            "format_id": "bestaudio[ext=m4a]/bestaudio/best",
            "label": "🎵 Audio Only (MP3)"
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
def download_video(req: DownloadRequest):
    for f in glob.glob("/tmp/sheikh_dl_temp.*"):
        try:
            os.remove(f)
        except:
            pass

    is_audio = ("bestaudio" in req.format_id and
                "bestvideo" not in req.format_id and
                req.format_id != "bestvideo+bestaudio/best")

    opts = get_base_opts()
    opts["format"] = req.format_id
    opts["outtmpl"] = "/tmp/sheikh_dl_temp.%(ext)s"
    opts["merge_output_format"] = "mp4"

    if is_audio:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
            raw_title = info.get("title", "sheikh_dl_video") if info else "sheikh_dl_video"

        safe_title = sanitize_filename(raw_title)
        files = glob.glob("/tmp/sheikh_dl_temp.*")

        if not files:
            return JSONResponse(status_code=500, content={"error": "Download failed, file not found."})

        filepath = files[0]
        actual_ext = filepath.rsplit(".", 1)[-1]
        filename = f"{safe_title}.{actual_ext}"

        def file_stream():
            with open(filepath, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
            try:
                os.remove(filepath)
            except:
                pass

        media_type = "audio/mpeg" if actual_ext == "mp3" else f"video/{actual_ext}"

        return StreamingResponse(
            file_stream(),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
