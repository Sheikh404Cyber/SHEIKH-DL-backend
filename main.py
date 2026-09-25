import os, sys, subprocess, shutil, platform, zipfile
import urllib.request, asyncio, tempfile, glob, re, json, time
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel


# ─────────────────────────────────────────
# STARTUP HELPERS
# ─────────────────────────────────────────

def run_cmd(cmd: list, timeout: int = 180, cwd=None) -> tuple:
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except Exception as e:
        return -1, "", str(e)


def update_ytdlp():
    print("⏳ Updating yt-dlp nightly + bgutil...")
    code, _, err = run_cmd([
        sys.executable, "-m", "pip", "install", "-U", "--pre",
        "yt-dlp[default]", "bgutil-ytdlp-pot-provider"
    ], timeout=180)
    print("✅ yt-dlp updated" if code == 0 else f"⚠️ yt-dlp warn: {err[-150:]}")


def install_nodejs():
    """Install Node.js for bgutil PO token server."""
    if shutil.which("node"):
        print(f"✅ Node.js: {shutil.which('node')}")
        return True

    print("⏳ Installing Node.js...")
    arch = platform.machine().lower()
    node_url = (
        "https://nodejs.org/dist/v20.18.1/node-v20.18.1-linux-arm64.tar.gz"
        if ("aarch64" in arch or "arm64" in arch)
        else "https://nodejs.org/dist/v20.18.1/node-v20.18.1-linux-x64.tar.gz"
    )
    node_dir = "node-v20.18.1-linux-arm64" if ("aarch64" in arch or "arm64" in arch) else "node-v20.18.1-linux-x64"

    try:
        import tarfile
        tar_path = "/tmp/node.tar.gz"
        urllib.request.urlretrieve(node_url, tar_path)
        with tarfile.open(tar_path, "r:gz") as t:
            t.extractall("/tmp/node_install")
        node_bin = f"/tmp/node_install/{node_dir}/bin"
        os.environ["PATH"] = node_bin + ":" + os.environ.get("PATH", "")
        for b in ["node", "npm", "npx"]:
            src = os.path.join(node_bin, b)
            dst = f"/tmp/{b}"
            if os.path.exists(src):
                shutil.copy2(src, dst)
                os.chmod(dst, 0o755)
        if shutil.which("node") or os.path.exists("/tmp/node"):
            os.environ["PATH"] = "/tmp:" + os.environ.get("PATH", "")
            print("✅ Node.js installed")
            return True
    except Exception as e:
        print(f"⚠️ Node.js install failed: {e}")
    return False


def setup_bgutil_pot_server():
    """Clone + build + start bgutil PO Token HTTP server on port 4416."""
    pid_file = "/tmp/bgutil.pid"
    server_dir = "/tmp/bgutil-server"

    # Check if already running
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            print(f"✅ bgutil POT server already running PID={pid}")
            os.environ["YT_DLP_POT_PROVIDER_URL"] = "http://127.0.0.1:4416"
            return True
        except Exception:
            pass

    node_bin = shutil.which("node") or "/tmp/node"
    if not os.path.exists(node_bin):
        print("⚠️ No Node.js — skipping bgutil server")
        return False

    print("⏳ Cloning bgutil POT server...")
    if not os.path.exists(server_dir):
        code, _, err = run_cmd([
            "git", "clone", "--depth=1",
            "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git",
            server_dir
        ], timeout=120)
        if code != 0:
            print(f"⚠️ Clone failed: {err[-150:]}")
            return False

    srv = os.path.join(server_dir, "server")
    nm = os.path.join(srv, "node_modules")
    build = os.path.join(srv, "build", "main.js")

    if not os.path.exists(nm):
        print("⏳ npm install...")
        run_cmd([node_bin, os.path.join(shutil.which("npm") or "/tmp/npm", ""), "ci"],
                timeout=180, cwd=srv)
        # fallback
        if not os.path.exists(nm):
            run_cmd([shutil.which("npm") or "/tmp/npm", "install", "--ignore-scripts"],
                    timeout=180, cwd=srv)

    if not os.path.exists(build):
        print("⏳ npx tsc...")
        run_cmd([shutil.which("npx") or "/tmp/npx", "tsc"], timeout=120, cwd=srv)

    if not os.path.exists(build):
        print("⚠️ bgutil build failed — TypeScript compile error")
        return False

    proc = subprocess.Popen(
        [node_bin, build, "--port", "4416"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=srv
    )
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))

    time.sleep(3)  # wait for server to bind

    # Verify server is up
    try:
        with urllib.request.urlopen("http://127.0.0.1:4416", timeout=3):
            pass
    except Exception:
        pass  # server may return error but still be running

    os.environ["YT_DLP_POT_PROVIDER_URL"] = "http://127.0.0.1:4416"
    print(f"✅ bgutil POT server running PID={proc.pid}")
    return True


def install_deno():
    deno_path = "/tmp/deno"
    if os.path.exists(deno_path):
        os.environ["PATH"] = "/tmp:" + os.environ.get("PATH", "")
        print("✅ Deno already at /tmp/deno")
        return
    print("⏳ Installing Deno...")
    arch = platform.machine().lower()
    url = (
        "https://github.com/denoland/deno/releases/latest/download/deno-aarch64-unknown-linux-gnu.zip"
        if ("aarch64" in arch or "arm64" in arch)
        else "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"
    )
    try:
        urllib.request.urlretrieve(url, "/tmp/deno_dl.zip")
        with zipfile.ZipFile("/tmp/deno_dl.zip", "r") as z:
            z.extractall("/tmp")
        os.chmod("/tmp/deno", 0o755)
        os.environ["PATH"] = "/tmp:" + os.environ.get("PATH", "")
        print("✅ Deno installed")
    except Exception as e:
        print(f"⚠️ Deno install failed: {e}")


def copy_cookies():
    src, dst = "/etc/secrets/cookies.txt", "/tmp/cookies.txt"
    if os.path.exists(src) and not os.path.exists(dst):
        shutil.copy2(src, dst)
        print("✅ Cookies copied to /tmp")
    elif os.path.exists(dst):
        print("✅ Cookies at /tmp/cookies.txt")


def get_cookie_file():
    for p in ["/tmp/cookies.txt", "/etc/secrets/cookies.txt"]:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None


# ─────────────────────────────────────────
# VIDEO ID EXTRACT
# ─────────────────────────────────────────

def extract_video_id(url: str):
    m = re.search(r"(?:v=|youtu\.be/|embed/|shorts/)([a-zA-Z0-9_-]{11})", url)
    return m.group(1) if m else None


# ─────────────────────────────────────────
# OEMBED - FREE METADATA (NEVER BLOCKED)
# ─────────────────────────────────────────

def get_oembed_info(url: str) -> dict | None:
    """YouTube oEmbed API — unlimited, never blocked by datacenter IPs."""
    try:
        oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
        with urllib.request.urlopen(oembed_url, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"⚠️ oEmbed failed: {e}")
        return None


# ─────────────────────────────────────────
# YT-DLP OPTIONS (battle-tested clients)
# ─────────────────────────────────────────

# Player client rotation strategies (most reliable first)
CLIENT_STRATEGIES = [
    ["tv_downgraded", "web_embedded"],
    ["android_vr", "tv_downgraded"],
    ["web_creator", "tv_downgraded"],
    ["mweb", "tv_downgraded"],
    ["tv_downgraded"],
]


def get_ytdlp_opts(client_index: int = 0) -> dict:
    clients = CLIENT_STRATEGIES[client_index % len(CLIENT_STRATEGIES)]
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "nocheckcertificate": True,
        "force_ipv4": True,
        "extractor_args": {
            "youtube": {
                "player_client": clients,
                "player_skip": ["webpage"],
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
    install_nodejs()
    install_deno()
    copy_cookies()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, setup_bgutil_pot_server)
    yield


app = FastAPI(title="SHEIKH Downloader API", version="7.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                  allow_methods=["*"], allow_headers=["*"])


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
    pot_url = os.environ.get("YT_DLP_POT_PROVIDER_URL", "not set")
    return {
        "status": "SHEIKH Downloader API v7.0 🚀",
        "pot_provider": pot_url,
    }


@app.get("/check")
def check():
    import importlib.metadata as meta
    try:
        ytdlp_ver = meta.version("yt-dlp")
    except Exception:
        ytdlp_ver = "unknown"
    try:
        bgutil_ver = meta.version("bgutil-ytdlp-pot-provider")
    except Exception:
        bgutil_ver = "not installed"

    bgutil_server = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:4416", timeout=2):
            bgutil_server = True
    except Exception:
        # Server might be running but return non-200
        bgutil_pid = os.path.exists("/tmp/bgutil.pid")
        bgutil_server = bgutil_pid

    return {
        "status": "ok",
        "yt_dlp_version": ytdlp_ver,
        "bgutil_plugin": bgutil_ver,
        "bgutil_server": bgutil_server,
        "pot_provider_url": os.environ.get("YT_DLP_POT_PROVIDER_URL", "not set"),
        "node_found": bool(shutil.which("node") or os.path.exists("/tmp/node")),
        "deno_found": os.path.exists("/tmp/deno"),
        "secret_cookies": os.path.exists("/etc/secrets/cookies.txt"),
        "tmp_cookies": os.path.exists("/tmp/cookies.txt"),
    }


@app.post("/info")
async def get_info(req: URLRequest):
    loop = asyncio.get_event_loop()

    # ── Layer 1: oEmbed (metadata only, never blocked) ──
    oembed = await loop.run_in_executor(None, get_oembed_info, req.url)

    # ── Layer 2: yt-dlp with client rotation ──
    last_error = ""
    for i in range(len(CLIENT_STRATEGIES)):
        try:
            opts = get_ytdlp_opts(i)
            opts["skip_download"] = True

            def _extract(opts=opts):
                import yt_dlp
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(req.url, download=False)

            info = await asyncio.wait_for(
                loop.run_in_executor(None, _extract),
                timeout=60,
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
                "source": f"yt-dlp (clients: {CLIENT_STRATEGIES[i]})",
                "title": info.get("title") or (oembed.get("title") if oembed else ""),
                "thumbnail": info.get("thumbnail") or (oembed.get("thumbnail_url") if oembed else ""),
                "duration": info.get("duration"),
                "uploader": info.get("uploader") or (oembed.get("author_name") if oembed else ""),
                "view_count": info.get("view_count"),
                "formats": formats,
            }
        except asyncio.TimeoutError:
            last_error = f"Timeout on strategy {i}"
            print(f"⚠️ {last_error}")
            continue
        except Exception as e:
            last_error = str(e)
            print(f"⚠️ Strategy {i} failed: {last_error[:100]}")
            continue

    # If yt-dlp completely failed but we have oembed
    if oembed:
        return {
            "source": "oembed_only",
            "title": oembed.get("title", ""),
            "thumbnail": oembed.get("thumbnail_url", ""),
            "duration": None,
            "uploader": oembed.get("author_name", ""),
            "formats": [],
            "warning": f"Full info failed: {last_error[:200]}",
        }

    raise HTTPException(status_code=400, detail=f"All strategies failed: {last_error}")


@app.post("/download")
async def download_video(req: DownloadRequest):
    loop = asyncio.get_event_loop()
    last_error = ""

    for i in range(len(CLIENT_STRATEGIES)):
        try:
            import yt_dlp
            opts = get_ytdlp_opts(i)

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
                if req.format_id and req.format_id not in ["best", ""]:
                    opts["format"] = req.format_id
                else:
                    opts["format"] = "bestvideo*+bestaudio/best"
                opts["merge_output_format"] = "mp4"

            opts["outtmpl"] = out_tmpl

            def _download(opts=opts):
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

            if filename and os.path.exists(filename) and os.path.getsize(filename) > 0:
                return FileResponse(
                    path=filename,
                    filename=os.path.basename(filename),
                    media_type="application/octet-stream",
                )

        except asyncio.TimeoutError:
            last_error = f"Timeout on strategy {i}"
            print(f"⚠️ {last_error}")
            continue
        except Exception as e:
            last_error = str(e)
            print(f"⚠️ Download strategy {i} failed: {last_error[:100]}")
            continue

    raise HTTPException(status_code=400, detail=f"All download strategies failed: {last_error}")
