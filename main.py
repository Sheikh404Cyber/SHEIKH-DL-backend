import os
import sys
import subprocess
import shutil
import platform
import zipfile
import urllib.request
import asyncio
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─────────────────────────────────────────
# STARTUP HELPERS
# ─────────────────────────────────────────

def run_cmd(cmd: list[str], timeout: int = 180) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return result.returncode, result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except Exception as e:
        return -1, "", str(e)


def update_ytdlp():
    """Always install latest nightly yt-dlp + bgutil plugin."""
    print("⏳ Updating yt-dlp nightly + bgutil plugin...")
    code, out, err = run_cmd([
        sys.executable, "-m", "pip", "install", "-U", "--pre",
        "yt-dlp[default]", "bgutil-ytdlp-pot-provider"
    ], timeout=180)
    if code == 0:
        print("✅ yt-dlp nightly + bgutil updated successfully")
    else:
        print(f"⚠️  pip update warning (non-fatal): {err[-300:]}")


def install_node():
    """Install Node.js if not present (needed by bgutil script provider)."""
    if shutil.which("node"):
        print(f"✅ Node.js already available: {shutil.which('node')}")
        return

    print("⏳ Installing Node.js...")
    arch = platform.machine().lower()

    # Try apt-get first (Debian/Ubuntu based)
    code, _, _ = run_cmd(["apt-get", "install", "-y", "nodejs", "npm"], timeout=120)
    if code == 0 and shutil.which("node"):
        print("✅ Node.js installed via apt-get")
        return

    # Fallback: download NodeJS binary
    if "x86_64" in arch or "amd64" in arch:
        node_url = "https://nodejs.org/dist/v20.18.0/node-v20.18.0-linux-x64.tar.gz"
        node_dir = "node-v20.18.0-linux-x64"
    else:
        node_url = "https://nodejs.org/dist/v20.18.0/node-v20.18.0-linux-arm64.tar.gz"
        node_dir = "node-v20.18.0-linux-arm64"

    try:
        import tarfile
        dest = "/tmp/node.tar.gz"
        urllib.request.urlretrieve(node_url, dest)
        with tarfile.open(dest, "r:gz") as t:
            t.extractall("/tmp/node_bin")
        node_path = f"/tmp/node_bin/{node_dir}/bin"
        # Add to PATH
        os.environ["PATH"] = node_path + ":" + os.environ.get("PATH", "")
        # Create symlinks
        for binary in ["node", "npm", "npx"]:
            src = os.path.join(node_path, binary)
            dst = f"/tmp/{binary}"
            if os.path.exists(src):
                shutil.copy2(src, dst)
                os.chmod(dst, 0o755)
        if shutil.which("node") or os.path.exists("/tmp/node"):
            print("✅ Node.js installed from binary")
        else:
            print("⚠️  Node.js install failed (non-fatal)")
    except Exception as e:
        print(f"⚠️  Node.js install error (non-fatal): {e}")


def install_deno():
    """Install Deno if not present."""
    deno_path = "/tmp/deno"
    if os.path.exists(deno_path):
        os.environ["PATH"] = "/tmp:" + os.environ.get("PATH", "")
        print(f"✅ Deno already at {deno_path}")
        return

    print("⏳ Installing Deno...")
    arch = platform.machine().lower()
    if "aarch64" in arch or "arm64" in arch:
        deno_zip_url = "https://github.com/denoland/deno/releases/latest/download/deno-aarch64-unknown-linux-gnu.zip"
    else:
        deno_zip_url = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"

    try:
        zip_path = "/tmp/deno_dl.zip"
        urllib.request.urlretrieve(deno_zip_url, zip_path)
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall("/tmp")
        os.chmod(deno_path, 0o755)
        os.environ["PATH"] = "/tmp:" + os.environ.get("PATH", "")
        print(f"✅ Deno installed at {deno_path}")
    except Exception as e:
        print(f"⚠️  Deno install failed (non-fatal): {e}")


def setup_bgutil_server():
    """Clone bgutil server repo and run it in background on port 4416."""
    server_dir = "/tmp/bgutil-server"
    pid_file = "/tmp/bgutil.pid"

    # Check if already running
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # Check if process alive
            print(f"✅ bgutil HTTP server already running (PID {pid})")
            return
        except (OSError, ProcessLookupError, ValueError):
            pass  # Process dead, restart

    print("⏳ Setting up bgutil POT server...")

    # Determine JS runtime
    node_bin = shutil.which("node") or "/tmp/node"
    deno_bin = shutil.which("deno") or "/tmp/deno"
    use_deno = os.path.exists(deno_bin)
    use_node = os.path.exists(node_bin)

    if not use_node and not use_deno:
        print("⚠️  No JS runtime available for bgutil server (non-fatal)")
        return

    # Clone repo if needed
    if not os.path.exists(server_dir):
        code, _, err = run_cmd([
            "git", "clone", "--depth=1",
            "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git",
            server_dir
        ], timeout=120)
        if code != 0:
            print(f"⚠️  bgutil clone failed (non-fatal): {err[-200:]}")
            return

    server_src = os.path.join(server_dir, "server")

    if use_node:
        # Install npm deps
        if not os.path.exists(os.path.join(server_src, "node_modules")):
            code, _, err = run_cmd(["npm", "ci"], timeout=180)
            if code != 0:
                # Try npm install as fallback
                os.chdir(server_src)
                run_cmd(["npm", "install", "--ignore-scripts"], timeout=180)

        # Transpile TypeScript
        build_dir = os.path.join(server_src, "build")
        if not os.path.exists(build_dir):
            os.chdir(server_src)
            run_cmd(["npx", "tsc"], timeout=120)

        # Start server
        build_main = os.path.join(server_src, "build", "main.js")
        if os.path.exists(build_main):
            proc = subprocess.Popen(
                [node_bin, build_main, "--port", "4416"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=server_src,
            )
            with open(pid_file, "w") as f:
                f.write(str(proc.pid))
            print(f"✅ bgutil HTTP server started with Node.js (PID {proc.pid})")
            return

    if use_deno:
        # Start with deno
        main_ts = os.path.join(server_src, "src", "main.ts")
        if os.path.exists(main_ts):
            proc = subprocess.Popen(
                [
                    deno_bin, "run",
                    "--allow-env", "--allow-net",
                    f"--allow-ffi={server_src}/node_modules",
                    f"--allow-read={server_src}",
                    main_ts, "--port", "4416"
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=server_src,
            )
            with open(pid_file, "w") as f:
                f.write(str(proc.pid))
            print(f"✅ bgutil HTTP server started with Deno (PID {proc.pid})")
            return

    print("⚠️  bgutil server could not start (non-fatal, will use script fallback)")


def copy_cookies():
    """Copy cookies from Render secret to /tmp."""
    src = "/etc/secrets/cookies.txt"
    dst = "/tmp/cookies.txt"
    if os.path.exists(src) and not os.path.exists(dst):
        try:
            shutil.copy2(src, dst)
            print(f"✅ Cookies copied: {src} → {dst}")
        except Exception as e:
            print(f"⚠️  Cookie copy failed: {e}")
    elif os.path.exists(dst):
        print("✅ Cookies already at /tmp/cookies.txt")
    else:
        print("ℹ️  No cookies file found (optional)")


def get_cookie_file() -> str | None:
    for p in ["/tmp/cookies.txt", "/etc/secrets/cookies.txt"]:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None


# ─────────────────────────────────────────
# YT-DLP OPTIONS
# ─────────────────────────────────────────

def get_base_opts() -> dict:
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
                "skip": ["dash", "hls"],
            }
        },
    }

    cookie_file = get_cookie_file()
    if cookie_file:
        opts["cookiefile"] = cookie_file

    return opts


# ─────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run all setup on startup
    update_ytdlp()
    install_node()
    install_deno()
    copy_cookies()
    # Start bgutil server in background thread to not block startup
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, setup_bgutil_server)
    # Small wait for server to bind
    await asyncio.sleep(3)
    yield


app = FastAPI(
    title="SHEIKH Downloader API",
    version="4.0.0",
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
    return {"status": "SHEIKH Downloader API v4.0 is running 🚀"}


@app.get("/check")
def check():
    import importlib.metadata as meta
    node_bin = shutil.which("node") or "/tmp/node"
    deno_bin = shutil.which("deno") or "/tmp/deno"

    try:
        ytdlp_version = meta.version("yt-dlp")
    except Exception:
        ytdlp_version = "unknown"

    try:
        bgutil_version = meta.version("bgutil-ytdlp-pot-provider")
    except Exception:
        bgutil_version = "not installed"

    bgutil_running = False
    try:
        import urllib.request as ur
        with ur.urlopen("http://127.0.0.1:4416", timeout=2) as r:
            bgutil_running = True
    except Exception:
        pass

    return {
        "status": "ok",
        "yt_dlp_version": ytdlp_version,
        "bgutil_plugin_version": bgutil_version,
        "bgutil_server_running": bgutil_running,
        "node_found": os.path.exists(node_bin),
        "deno_found": os.path.exists(deno_bin),
        "secret_cookies": os.path.exists("/etc/secrets/cookies.txt"),
        "tmp_cookies": os.path.exists("/tmp/cookies.txt"),
        "path": os.environ.get("PATH", ""),
    }


@app.post("/info")
async def get_info(req: URLRequest):
    try:
        import yt_dlp

        opts = get_base_opts()
        opts["skip_download"] = True

        loop = asyncio.get_event_loop()

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
                "tbr": f.get("tbr"),
            })

        return {
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
            "view_count": info.get("view_count"),
            "formats": formats,
        }

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Request timed out. Try again.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/download")
async def download_video(req: DownloadRequest):
    try:
        import yt_dlp
        from fastapi.responses import FileResponse

        opts = get_base_opts()

        with tempfile.NamedTemporaryFile(
            suffix=".%(ext)s", prefix="sheikh_dl_", dir="/tmp", delete=False
        ) as tmp:
            out_tmpl = tmp.name.replace(".%(ext)s", "") + ".%(ext)s"

        if req.audio_only:
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        else:
            opts["format"] = req.format_id if req.format_id != "best" else "bestvideo+bestaudio/best"

        opts["outtmpl"] = out_tmpl

        loop = asyncio.get_event_loop()

        def _download():
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(req.url, download=True)
                return ydl.prepare_filename(info)

        filename = await asyncio.wait_for(
            loop.run_in_executor(None, _download),
            timeout=300,
        )

        if not os.path.exists(filename):
            # Try finding the file with glob
            import glob
            base = out_tmpl.replace(".%(ext)s", "")
            matches = glob.glob(base + ".*")
            if matches:
                filename = matches[0]
            else:
                raise HTTPException(status_code=500, detail="Downloaded file not found")

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
