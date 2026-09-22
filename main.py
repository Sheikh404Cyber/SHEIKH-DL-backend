from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yt_dlp
import re
import os
import glob

app = FastAPI(title="SHEIKH-DL Backend")

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

def get_ydl_opts(extra={}):
    opts = {
        "quiet": True,
        "no_warnings": True,
        # tv_simply and android_vr do NOT require PO Token
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_simply", "android_vr", "android"],
                "skip": ["hls", "dash"],
            }
        },
        "http_headers": {
            "User-Agent": "com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }
    opts.update(extra)
    return opts

@app.get("/")
def root():
    return {"status": "SHEIKH-DL Backend is running!"}


@app.post("/info")
def get_video_info(req: VideoRequest):
    ydl_opts = get_ydl_opts({"skip_download": True})

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)

        formats_raw = info.get("formats", [])
        seen = set()
        formats = []

        formats.append({
            "format_id": "bestvideo+bestaudio/best",
            "label": "🏆 Best Quality (Auto)"
        })

        for fmt in reversed(formats_raw):
            fid = fmt.get("format_id")
            height = fmt.get("height")
            vcodec = fmt.get("vcodec", "none")
            ext = fmt.get("ext", "")

            if ext in ["mhtml", "none"]:
                continue
            if not fid:
                continue

            if vcodec != "none" and height and height not in seen:
                seen.add(height)
                formats.append({
                    "format_id": f"{fid}+bestaudio/best",
                    "label": get_format_label(fmt)
                })

        formats.append({
            "format_id": "bestaudio",
            "label": "🎵 Audio Only (MP3)"
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
    filename_holder = {"name": "video.mp4"}

    ydl_opts = get_ydl_opts({
        "format": req.format_id,
        "outtmpl": "/tmp/sheikh_dl_temp.%(ext)s",
        "merge_output_format": "mp4",
    })

    if req.format_id == "bestaudio":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    try:
        for f in glob.glob("/tmp/sheikh_dl_temp.*"):
            os.remove(f)

        with yt_dlp.YoutubeDL(get_ydl_opts({"skip_download": True})) as ydl:
            info = ydl.extract_info(req.url, download=False)
            title = sanitize_filename(info.get("title", "video"))

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([req.url])

        files = glob.glob("/tmp/sheikh_dl_temp.*")
        if not files:
            raise HTTPException(
                status_code=500,
                detail={"error": "Download failed, file not found"}
            )

        filepath = files[0]
        ext = filepath.split(".")[-1]

        if req.format_id == "bestaudio":
            filename_holder["name"] = f"{title}.mp3"
        else:
            filename_holder["name"] = f"{title}.{ext}"

        mime = "audio/mpeg" if ext == "mp3" else "video/mp4"

        def file_stream():
            with open(filepath, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
            os.remove(filepath)

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
