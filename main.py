from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yt_dlp
import io
import re

app = FastAPI(title="SHEIKH-DL Backend")

# ==================== CORS ====================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== MODELS ====================
class VideoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str

# ==================== HELPERS ====================
def sanitize_filename(name):
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name[:80].strip()

def get_format_label(fmt):
    label_parts = []

    ext = fmt.get("ext", "")
    vcodec = fmt.get("vcodec", "none")
    acodec = fmt.get("acodec", "none")
    height = fmt.get("height")
    filesize = fmt.get("filesize") or fmt.get("filesize_approx")

    if vcodec != "none" and height:
        label_parts.append(f"{height}p")
    elif vcodec == "none" and acodec != "none":
        label_parts.append("Audio Only")

    if ext:
        label_parts.append(f"({ext.upper()})")

    if filesize:
        size_mb = filesize / (1024 * 1024)
        label_parts.append(f"~ {size_mb:.1f} MB")

    return " ".join(label_parts) if label_parts else "Unknown Format"

# ==================== ROUTES ====================

@app.get("/")
def root():
    return {"status": "SHEIKH-DL Backend is running!"}


@app.post("/info")
def get_video_info(req: VideoRequest):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)

        formats_raw = info.get("formats", [])
        seen = set()
        formats = []

        # Best video+audio combined (auto)
        formats.append({
            "format_id": "bestvideo+bestaudio/best",
            "label": "Best Quality (Auto)"
        })

        # Filter useful formats
        for fmt in reversed(formats_raw):
            fid = fmt.get("format_id")
            height = fmt.get("height")
            vcodec = fmt.get("vcodec", "none")
            acodec = fmt.get("acodec", "none")
            ext = fmt.get("ext", "")

            # Skip useless formats
            if ext in ["mhtml", "none"]:
                continue
            if not fid:
                continue

            # Video formats
            if vcodec != "none" and height and height not in seen:
                seen.add(height)
                formats.append({
                    "format_id": f"{fid}+bestaudio/best",
                    "label": get_format_label(fmt)
                })

        # Audio only
        formats.append({
            "format_id": "bestaudio",
            "label": "Audio Only (MP3)"
        })

        return {
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader", ""),
            "formats": formats
        }

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail={"error": str(e)})
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.post("/download")
def download_video(req: DownloadRequest):
    buffer = io.BytesIO()
    filename_holder = {"name": "video.mp4"}

    def ydl_hook(d):
        pass

    ydl_opts = {
        "format": req.format_id,
        "quiet": True,
        "no_warnings": True,
        "outtmpl": "-",
        "logtostderr": False,
    }

    # Audio only — convert to mp3
    if req.format_id == "bestaudio":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
        filename_holder["name"] = "audio.mp3"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
            title = sanitize_filename(info.get("title", "video"))

            if req.format_id == "bestaudio":
                filename_holder["name"] = f"{title}.mp3"
            else:
                ext = info.get("ext", "mp4")
                filename_holder["name"] = f"{title}.{ext}"

        # Download to buffer
        ydl_opts["outtmpl"] = "/tmp/sheikh_dl_temp.%(ext)s"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([req.url])

        import os
        import glob

        # Find downloaded file
        files = glob.glob("/tmp/sheikh_dl_temp.*")
        if not files:
            raise HTTPException(status_code=500, detail={"error": "Download failed"})

        filepath = files[0]
        ext = filepath.split(".")[-1]

        if req.format_id == "bestaudio":
            filename_holder["name"] = f"{title}.mp3"
        else:
            filename_holder["name"] = f"{title}.{ext}"

        # Stream file
        def file_stream():
            with open(filepath, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
            os.remove(filepath)

        mime = "audio/mpeg" if ext == "mp3" else "video/mp4"

        return StreamingResponse(
            file_stream(),
            media_type=mime,
            headers={
                "Content-Disposition": f'attachment; filename="{filename_holder["name"]}"'
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e)})
