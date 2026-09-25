import os
import sys
import subprocess
import asyncio
import zipfile
import stat
import platform
import shutil
import signal
import requests
import logging
import re
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ───────── CONSTANTS ─────────
BGUTIL_RS_BINARY = "/tmp/bgutil-pot"
BGUTIL_RS_PLUGIN_DIR = str(Path.home() / "yt-dlp-plugins" / "bgutil-ytdlp-pot-provider")
BGUTIL_SERVER_PORT = 4416
BGUTIL_PID_FILE = "/tmp/bgutil.pid"
DENO_PATH = "/tmp/deno"
COOKIES_SRC = "/etc/secrets/cookies.txt"
COOKIES_DST = "/tmp/cookies.txt"
ARCH = platform.machine().lower()

# ───────── STEP 1: pip uninstall OLD bgutil plugin (prevent conflict) ─────────
def remove_pip_bgutil():
    """Remove pip-installed bgutil plugin to prevent conflict with Rust plugin."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "bgutil-ytdlp-pot-provider"],
            capture_output=True, text=True, timeout=60
        )
        if "Successfully uninstalled" in result.stdout:
            logger.info("✅ Removed pip bgutil plugin (conflict prevention)")
        else:
            logger.info("ℹ️ pip bgutil plugin was not installed (OK)")
    except Exception as e:
        logger.warning(f"⚠️ Could not remove pip bgutil: {e}")

# ───────── STEP 2: Update yt-dlp ─────────
def update_ytdlp():
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "--pre", "yt-dlp[default]"],
            capture_output=True, timeout=180
        )
        logger.info("✅ yt-dlp updated")
    except Exception as e:
        logger.warning(f"⚠️ yt-dlp update failed: {e}")

# ───────── STEP 3: Install Deno ─────────
def install_deno():
    if os.path.exists(DENO_PATH):
        logger.info("✅ Deno already installed")
        return True
    try:
        if "aarch64" in ARCH or "arm64" in ARCH:
            url = "https://github.com/denoland/deno/releases/latest/download/deno-aarch64-unknown-linux-gnu.zip"
        else:
            url = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"
        
        resp = requests.get(url, timeout=120, stream=True)
        zip_path = "/tmp/deno.zip"
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)
        
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extract("deno", "/tmp/")
        
        os.chmod(DENO_PATH, os.stat(DENO_PATH).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["PATH"] = f"/tmp:{os.environ.get('PATH', '')}"
        logger.info("✅ Deno installed")
        return True
    except Exception as e:
        logger.warning(f"⚠️ Deno install failed: {e}")
        return False

# ───────── STEP 4: Download Rust bgutil binary ─────────
def install_bgutil_rs_binary():
    if os.path.exists(BGUTIL_RS_BINARY) and os.access(BGUTIL_RS_BINARY, os.X_OK):
        logger.info(f"✅ bgutil-pot binary already exists at {BGUTIL_RS_BINARY}")
        return True
    try:
        if "aarch64" in ARCH or "arm64" in ARCH:
            url = "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-linux-aarch64"
        else:
            url = "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-linux-x86_64"
        
        logger.info(f"⬇️ Downloading bgutil-pot binary from {url}")
        resp = requests.get(url, timeout=120, stream=True, allow_redirects=True)
        resp.raise_for_status()
        
        with open(BGUTIL_RS_BINARY, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)
        
        os.chmod(BGUTIL_RS_BINARY, 0o755)
        logger.info(f"✅ bgutil-pot binary installed at {BGUTIL_RS_BINARY}")
        return True
    except Exception as e:
        logger.error(f"❌ bgutil-pot binary download failed: {e}")
        return False

# ───────── STEP 5: Install Rust bgutil yt-dlp PLUGIN ─────────
def install_bgutil_rs_plugin():
    """
    Install the Rust bgutil plugin zip into ~/yt-dlp-plugins/bgutil-ytdlp-pot-provider/
    This replaces both the old pip plugin AND the old Rust plugin (bgutil-rs folder).
    """
    # Remove OLD Rust plugin dir if it exists (old name)
    old_rs_dir = Path.home() / ".config" / "yt-dlp" / "plugins" / "bgutil-rs"
    if old_rs_dir.exists():
        shutil.rmtree(str(old_rs_dir), ignore_errors=True)
        logger.info(f"🗑️ Removed old bgutil-rs plugin dir: {old_rs_dir}")

    # Check if correct plugin already installed
    plugin_check = Path(BGUTIL_RS_PLUGIN_DIR) / "yt_dlp_plugins" / "extractor" / "getpot_bgutil_http.py"
    if plugin_check.exists():
        logger.info(f"✅ Rust bgutil plugin already installed at {BGUTIL_RS_PLUGIN_DIR}")
        return True
    
    try:
        zip_url = "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-ytdlp-pot-provider-rs.zip"
        logger.info(f"⬇️ Downloading Rust bgutil plugin zip")
        resp = requests.get(zip_url, timeout=120, stream=True, allow_redirects=True)
        resp.raise_for_status()
        
        zip_path = "/tmp/bgutil-rs-plugin.zip"
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)
        
        # Extract to the correct yt-dlp plugin directory
        plugin_dir = Path(BGUTIL_RS_PLUGIN_DIR)
        plugin_dir.mkdir(parents=True, exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(str(plugin_dir))
        
        logger.info(f"✅ Rust bgutil plugin installed at {BGUTIL_RS_PLUGIN_DIR}")
        return True
    except Exception as e:
        logger.error(f"❌ Rust bgutil plugin install failed: {e}")
        return False

# ───────── STEP 6: Start bgutil HTTP server ─────────
def start_bgutil_server():
    # Kill existing server if running
    if os.path.exists(BGUTIL_PID_FILE):
        try:
            with open(BGUTIL_PID_FILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, signal.SIGTERM)
            logger.info(f"🔄 Killed old bgutil server PID={old_pid}")
        except Exception:
            pass
    
    if not os.path.exists(BGUTIL_RS_BINARY):
        logger.error("❌ bgutil-pot binary not found, cannot start server")
        return False
    
    try:
        proc = subprocess.Popen(
            [BGUTIL_RS_BINARY, "server", "--host", "127.0.0.1", "--port", str(BGUTIL_SERVER_PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True
        )
        
        with open(BGUTIL_PID_FILE, "w") as f:
            f.write(str(proc.pid))
        
        # Wait for server to be ready
        import time
        import socket
        for i in range(20):
            time.sleep(1)
            try:
                with socket.create_connection(("127.0.0.1", BGUTIL_SERVER_PORT), timeout=2):
                    logger.info(f"✅ bgutil-pot server ready on port {BGUTIL_SERVER_PORT} (PID={proc.pid})")
                    return True
            except Exception:
                continue
        
        logger.error("❌ bgutil-pot server did not start in time")
        return False
    except Exception as e:
        logger.error(f"❌ Failed to start bgutil server: {e}")
        return False

# ───────── STEP 7: Copy Cookies ─────────
def copy_cookies():
    try:
        if os.path.exists(COOKIES_SRC):
            shutil.copy2(COOKIES_SRC, COOKIES_DST)
            logger.info("✅ Cookies copied")
    except Exception as e:
        logger.warning(f"⚠️ Cookie copy failed: {e}")

# ───────── LIFESPAN ─────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Starting SHEIKH Downloader API v8.0.0")
    logger.info(f"🖥️ Architecture: {ARCH}")
    
    # Run startup tasks
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, remove_pip_bgutil)        # CRITICAL: remove conflict
    await loop.run_in_executor(None, update_ytdlp)
    await loop.run_in_executor(None, install_deno)
    await loop.run_in_executor(None, install_bgutil_rs_binary)
    await loop.run_in_executor(None, install_bgutil_rs_plugin)
    await loop.run_in_executor(None, start_bgutil_server)
    await loop.run_in_executor(None, copy_cookies)
    
    # Add /tmp to PATH for deno
    os.environ["PATH"] = f"/tmp:{os.environ.get('PATH', '')}"
    
    logger.info("=" * 50)
    logger.info("✅ Startup complete!")
    logger.info(f"🌐 Available at: https://sheikh-dl-backend.onrender.com")
    logger.info("=" * 50)
    
    yield

# ───────── FastAPI APP ─────────
app = FastAPI(
    title="SHEIKH Downloader API",
    version="8.0.0",
    lifespan=lifespan
)

# ───────── HELPERS ─────────
def ping_bgutil_server():
    try:
        r = requests.get(f"http://127.0.0.1:{BGUTIL_SERVER_PORT}/ping", timeout=3)
        return r.status_code == 200
    except Exception:
        return False

def make_ytdlp_opts(extra: dict = None) -> dict:
    """Build yt-dlp options with ONLY HTTP server method (no CLI conflict)."""
    opts = {
        "quiet": False,
        "no_warnings": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "nocheckcertificate": True,
        "force_ipv4": True,
        # ONLY use HTTP server — do NOT pass youtubepot-bgutilcli
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_downgraded", "web_embedded"],
                "player_skip": ["webpage"],
            },
            "youtubepot-bgutilhttp": {
                "base_url": f"http://127.0.0.1:{BGUTIL_SERVER_PORT}"
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if os.path.exists(COOKIES_DST):
        opts["cookiefile"] = COOKIES_DST
    if extra:
        opts.update(extra)
    return opts

# ───────── MODELS ─────────
class VideoRequest(BaseModel):
    url: str
    format_id: Optional[str] = None
    audio_only: Optional[bool] = False

# ───────── ROUTES ─────────
@app.get("/")
async def root():
    return {
        "status": "ok",
        "version": "8.0.0",
        "bgutil_server": ping_bgutil_server(),
        "bgutil_port": BGUTIL_SERVER_PORT,
        "arch": ARCH
    }

@app.get("/check")
async def check():
    import importlib.metadata as meta
    
    ytdlp_ver = "unknown"
    try:
        ytdlp_ver = meta.version("yt-dlp")
    except Exception:
        pass
    
    bgutil_plugin_ver = "unknown"
    try:
        bgutil_plugin_ver = meta.version("bgutil-ytdlp-pot-provider")
    except Exception:
        # Rust plugin doesn't have pip metadata, check file
        plugin_file = Path(BGUTIL_RS_PLUGIN_DIR) / "yt_dlp_plugins" / "extractor" / "getpot_bgutil_http.py"
        bgutil_plugin_ver = "rs-plugin-installed" if plugin_file.exists() else "not-installed"
    
    bgutil_bin = os.path.exists(BGUTIL_RS_BINARY) and os.access(BGUTIL_RS_BINARY, os.X_OK)
    bgutil_server = ping_bgutil_server()
    
    return {
        "status": "ok",
        "yt_dlp_version": ytdlp_ver,
        "bgutil_plugin": bgutil_plugin_ver,
        "bgutil_bin_exists": bgutil_bin,
        "bgutil_server_ping": bgutil_server,
        "bgutil_port": BGUTIL_SERVER_PORT,
        "deno_found": os.path.exists(DENO_PATH) or bool(shutil.which("deno")),
        "secret_cookies": os.path.exists(COOKIES_SRC),
        "tmp_cookies": os.path.exists(COOKIES_DST),
        "arch": ARCH,
        "plugin_dir": BGUTIL_RS_PLUGIN_DIR,
    }

@app.get("/debug/ytdlp")
async def debug_ytdlp():
    """Test yt-dlp with verbose output to verify POT provider is working."""
    logs = []
    
    class LogCapture:
        def debug(self, msg): logs.append(f"DBG: {msg}")
        def warning(self, msg): logs.append(f"WARN: {msg}")
        def error(self, msg): logs.append(f"ERR: {msg}")
    
    opts = make_ytdlp_opts({
        "verbose": True,
        "skip_download": True,
        "logger": LogCapture(),
    })
    
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                download=False
            )
        return {
            "success": True,
            "title": info.get("title", ""),
            "formats_count": len(info.get("formats", [])),
            "bgutil_running": ping_bgutil_server(),
            "logs": [l for l in logs if any(k in l.lower() for k in ["pot", "bgutil", "error", "warn", "player", "debug"])][:40]
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "bgutil_running": ping_bgutil_server(),
            "logs": logs[:60]
        }

@app.post("/info")
async def get_info(req: VideoRequest):
    # Try oEmbed first for basic info
    oembed_data = {}
    try:
        oembed_url = f"https://www.youtube.com/oembed?url={req.url}&format=json"
        r = requests.get(oembed_url, timeout=10)
        if r.status_code == 200:
            oembed_data = r.json()
    except Exception:
        pass
    
    # Try yt-dlp with multiple strategies
    strategies = [
        {"player_client": ["tv_downgraded", "web_embedded"], "player_skip": ["webpage"]},
        {"player_client": ["android_vr", "tv_downgraded"], "player_skip": ["webpage"]},
        {"player_client": ["web_creator", "tv_downgraded"]},
        {"player_client": ["mweb", "tv_downgraded"]},
        {"player_client": ["tv_downgraded"]},
    ]
    
    last_error = None
    for i, strategy in enumerate(strategies):
        try:
            opts = make_ytdlp_opts({
                "skip_download": True,
                "extractor_args": {
                    "youtube": strategy,
                    "youtubepot-bgutilhttp": {
                        "base_url": f"http://127.0.0.1:{BGUTIL_SERVER_PORT}"
                    }
                }
            })
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(req.url, download=False)
            
            formats = []
            for f in info.get("formats", []):
                if f.get("url"):
                    formats.append({
                        "format_id": f.get("format_id"),
                        "ext": f.get("ext"),
                        "resolution": f.get("resolution") or f.get("height"),
                        "filesize": f.get("filesize"),
                        "vcodec": f.get("vcodec"),
                        "acodec": f.get("acodec"),
                        "tbr": f.get("tbr"),
                    })
            
            return {
                "source": f"ytdlp_strategy_{i+1}",
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
                "view_count": info.get("view_count"),
                "formats": formats,
            }
        except Exception as e:
            last_error = str(e)
            continue
    
    # Fallback to oEmbed only
    if oembed_data:
        return {
            "source": "oembed_only",
            "title": oembed_data.get("title"),
            "thumbnail": oembed_data.get("thumbnail_url"),
            "author": oembed_data.get("author_name"),
            "formats": [],
            "error": last_error
        }
    
    raise HTTPException(status_code=500, detail=f"All strategies failed: {last_error}")

@app.post("/download")
async def download_video(req: VideoRequest):
    video_id_match = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", req.url)
    video_id = video_id_match.group(1) if video_id_match else "video"
    
    out_path = f"/tmp/sheikh_{video_id}"
    
    strategies = [
        {"player_client": ["tv_downgraded", "web_embedded"], "player_skip": ["webpage"]},
        {"player_client": ["android_vr", "tv_downgraded"], "player_skip": ["webpage"]},
        {"player_client": ["tv_downgraded"]},
    ]
    
    last_error = None
    for strategy in strategies:
        try:
            if req.audio_only:
                fmt = "bestaudio[ext=m4a]/bestaudio/best"
                ext = "m4a"
            elif req.format_id:
                fmt = f"{req.format_id}+bestaudio/best"
                ext = "mp4"
            else:
                fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
                ext = "mp4"
            
            final_path = f"{out_path}.{ext}"
            
            opts = make_ytdlp_opts({
                "format": fmt,
                "outtmpl": f"{out_path}.%(ext)s",
                "merge_output_format": "mp4" if not req.audio_only else None,
                "extractor_args": {
                    "youtube": strategy,
                    "youtubepot-bgutilhttp": {
                        "base_url": f"http://127.0.0.1:{BGUTIL_SERVER_PORT}"
                    }
                }
            })
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(req.url, download=True)
            
            # Find downloaded file
            for candidate in [final_path, f"{out_path}.webm", f"{out_path}.mkv"]:
                if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                    title = info.get("title", "video").replace("/", "_")[:50]
                    return FileResponse(
                        candidate,
                        media_type="video/mp4" if not req.audio_only else "audio/mp4",
                        filename=f"{title}.{ext}"
                    )
        except Exception as e:
            last_error = str(e)
            continue
    
    raise HTTPException(status_code=500, detail=f"Download failed: {last_error}")
