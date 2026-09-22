from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp
import os
import re
import glob

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
    return {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "mweb", "android"],
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "nocheckcertificate": True,
    }

@app.get("/")
def root():
    return {"status": "SHEIKH-DL Backend is running!"}

@app.post("/info")
def get_video_info(req: VideoRequest):
    opts = get_base_opts()
    opts.update({
        "skip_download": True,
        "listformats": False,
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=False)

        title = info.get("title", "Unknown Title")
        thumbnail = info.get("thumbnail", "https://via.placeholder.com/200x120?text=No+Thumbnail")
        duration = info.get("duration", 0)
        uploader = info.get("uploader", "Unknown")

        formats = []
        seen_heights = set()

        # Best quality (auto)
        formats.append({
            "format_id": "bestvideo+bestaudio/best",
            "label": "⭐ Best Quality (Auto)"
        })

        # Video formats by resolution
        raw_formats = info.get("formats", [])
        for f in reversed(raw_formats):
            height = f.get("height")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")

            if height and vcodec != "none" and height not in seen_heights:
                seen_heights.add(height)
                formats.append({
                    "format_id": f"{f['format_id']}+bestaudio/best",
                    "label": get_format_label(f)
                })

        # Audio only
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

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        return JSONResponse(
            status_code=400,
            content={"error": f"Could not fetch video info: {error_msg}"}
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Server error: {str(e)}"}
        )

@app.post("/download")
def download_video(req: DownloadRequest):
    # Clean up old temp files
    for f in glob.glob("/tmp/sheikh_dl_temp.*"):
        try:
            os.remove(f)
        except:
            pass

    is_audio = "bestaudio" in req.format_id and "bestvideo" not in req.format_id and req.format_id != "bestvideo+bestaudio/best"

    opts = get_base_opts()
    opts.update({
        "format": req.format_id,
        "outtmpl": "/tmp/sheikh_dl_temp.%(ext)s",
    })

    if is_audio:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
            raw_title = info.get("title", "sheikh_dl_video")

        safe_title = sanitize_filename(raw_title)
        ext = "mp3" if is_audio else "mp4"

        # Find the downloaded file
        files = glob.glob("/tmp/sheikh_dl_temp.*")
        if not files:
            return JSONResponse(status_code=500, content={"error": "Download failed, file not found."})

        filepath = files[0]
        actual_ext = filepath.split(".")[-1]
        filename = f"{safe_title}.{actual_ext}"

        def file_stream():
            with open(filepath, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
            try:
                os.remove(filepath)
            except:
                pass

        return StreamingResponse(
            file_stream(),
            media_type=f"video/{actual_ext}" if actual_ext != "mp3" else "audio/mpeg",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    except yt_dlp.utils.DownloadError as e:
        return JSONResponse(
            status_code=400,
            content={"error": f"Download error: {str(e)}"}
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Server error: {str(e)}"}
        )
