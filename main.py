import os, sys, subprocess, shutil, platform, zipfile
import urllib.request, asyncio, tempfile, glob, re, json, time
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel


# ─────────────────────────────────────────
# GLOBALS
# ─────────────────────────────────────────
BGUTIL_PORT    = 4416
BGUTIL_URL     = f"http://127.0.0.1:{BGUTIL_PORT}"
BGUTIL_BIN     = "/tmp/bgutil-pot"
_bgutil_running = False


def run_cmd(cmd, timeout=180, cwd=None, env=None):
    try:
        e = {**os.environ, **(env or {})}
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, cwd=cwd, env=e)
        return r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except Exception as ex:
        return -1, "", str(ex)


def get_cookie_file():
    for p in ["/tmp/cookies.txt", "/etc/secrets/cookies.txt"]:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None


def extract_video_id(url):
    m = re.search(r"(?:v=|youtu\.be/|embed/|shorts/)([a-zA-Z0-9_-]{11})", url)
    return m.group(1) if m else None


# ─────────────────────────────────────────
# STEP 1 — Update yt-dlp + plugin
# ─────────────────────────────────────────

def step_update_ytdlp():
    print("⏳ [1/4] Updating yt-dlp nightly + bgutil plugin...")
    code, _, err = run_cmd([
        sys.executable, "-m", "pip", "install", "-U", "--pre",
        "yt-dlp[default]", "bgutil-ytdlp-pot-provider"
    ], timeout=180)
    print("✅ yt-dlp + bgutil plugin updated" if code == 0
          else f"⚠️  pip warn: {err[-100:]}")


# ─────────────────────────────────────────
# STEP 2 — Download Rust bgutil binary
# ─────────────────────────────────────────

def step_install_bgutil_rs():
    """Download pre-compiled Rust bgutil-pot binary — no Node/npm/tsc needed."""
    global _bgutil_running

    # Check if already running
    pid_file = "/tmp/bgutil.pid"
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            _bgutil_running = True
            print(f"✅ [2/4] bgutil-pot server already running PID={pid}")
            return
        except Exception:
            pass

    # Download binary if not present
    if not os.path.exists(BGUTIL_BIN):
        print("⏳ [2/4] Downloading Rust bgutil-pot binary...")
        arch = platform.machine().lower()

        if "aarch64" in arch or "arm64" in arch:
            bin_url = "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-linux-aarch64"
        else:
            bin_url = "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-linux-x86_64"

        try:
            urllib.request.urlretrieve(bin_url, BGUTIL_BIN)
            os.chmod(BGUTIL_BIN, 0o755)
            print(f"✅ bgutil-pot binary downloaded to {BGUTIL_BIN}")
        except Exception as e:
            print(f"⚠️  bgutil-pot download failed: {e}")
            return
    else:
        print(f"✅ [2/4] bgutil-pot binary already at {BGUTIL_BIN}")

    # Also install the Rust plugin zip for yt-dlp
    _install_rs_plugin()

    # Start server
    _start_bgutil_server()


def _install_rs_plugin():
    """Install the Rust bgutil yt-dlp plugin (separate from Python plugin)."""
    plugin_dir = os.path.expanduser("~/.config/yt-dlp/plugins/bgutil-rs/yt_dlp_plugins/extractor")
    marker = os.path.join(plugin_dir, ".installed")
    if os.path.exists(marker):
        return

    print("  ⏳ Installing bgutil-rs yt-dlp plugin...")
    zip_url = "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-ytdlp-pot-provider-rs.zip"
    zip_path = "/tmp/bgutil-rs-plugin.zip"
    try:
        urllib.request.urlretrieve(zip_url, zip_path)
        os.makedirs(plugin_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(os.path.expanduser("~/.config/yt-dlp/plugins/bgutil-rs"))
        open(marker, "w").close()
        print("  ✅ bgutil-rs plugin installed")
    except Exception as e:
        print(f"  ⚠️  bgutil-rs plugin install failed (non-fatal): {e}")


def _start_bgutil_server():
    """Start bgutil-pot HTTP server in background."""
    global _bgutil_running
    pid_file = "/tmp/bgutil.pid"

    if not os.path.exists(BGUTIL_BIN):
        print("⚠️  bgutil-pot binary not found, skipping server start")
        return

    try:
        proc = subprocess.Popen(
            [BGUTIL_BIN, "server",
             "--host", "127.0.0.1",
             "--port", str(BGUTIL_PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with open(pid_file, "w") as f:
            f.write(str(proc.pid))

        # Wait up to 15s for /ping to respond
        for i in range(15):
            time.sleep(1)
            try:
                with urllib.request.urlopen(f"{BGUTIL_URL}/ping", timeout=2) as r:
                    if r.status == 200:
                        _bgutil_running = True
                        print(f"✅ bgutil-pot server ready on port {BGUTIL_PORT} (PID={proc.pid})")
                        return
            except Exception:
                pass

        # Server might still be starting
        _bgutil_running = True
        print(f"✅ bgutil-pot server started PID={proc.pid} (ping not yet ready)")

    except Exception as e:
        print(f"⚠️  bgutil-pot server start failed: {e}")


# ─────────────────────────────────────────
# STEP 3 — Deno (yt-dlp ejs fallback)
# ─────────────────────────────────────────

def step_install_deno():
    deno = "/tmp/deno"
    if os.path.exists(deno):
        os.environ["PATH"] = "/tmp:" + os.environ.get("PATH", "")
        print("✅ [3/4] Deno already installed")
        return
    print("⏳ [3/4] Installing Deno...")
    arch = platform.machine().lower()
    url = (
        "https://github.com/denoland/deno/releases/latest/download/deno-aarch64-unknown-linux-gnu.zip"
        if ("aarch64" in arch or "arm64" in arch)
        else "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"
    )
    try:
        urllib.request.urlretrieve(url, "/tmp/deno_dl.zip")
        with zipfile.ZipFile("/tmp/deno_dl.zip") as z:
            z.extractall("/tmp")
        os.chmod(deno, 0o755)
        os.environ["PATH"] = "/tmp:" + os.environ.get("PATH", "")
        print("✅ Deno installed")
    except Exception as e:
        print(f"⚠️  Deno install failed: {e}")


# ─────────────────────────────────────────
# STEP 4 — Cookies
# ─────────────────────────────────────────

def step_copy_cookies():
    print("⏳ [4/4] Setting up cookies...")
    src, dst = "/etc/secrets/cookies.txt", "/tmp/cookies.txt"
    if os.path.exists(src) and not os.path.exists(dst):
        shutil.copy2(src, dst)
        print("✅ Cookies copied")
    elif os.path.exists(dst):
        print("✅ Cookies at /tmp/cookies.txt")
    else:
        print("ℹ️  No cookies file")


# ─────────────────────────────────────────
# YT-DLP OPTIONS
# ─────────────────────────────────────────

CLIENT_COMBOS = [
    ["tv_downgraded", "web_embedded"],
    ["android_vr"],
    ["tv_downgraded"],
    ["mweb"],
    ["web_embedded"],
]


def make_ytdlp_opts(combo_index=0, extra=None):
    clients = CLIENT_COMBOS[combo_index % len(CLIENT_COMBOS)]

    ext_args = {
        "youtube": {
            "player_client": clients,
            "player_skip": ["webpage"],
        },
    }

    # Tell yt-dlp where bgutil HTTP server is
    if _bgutil_running:
        ext_args["youtubepot-bgutilhttp"] = {"base_url": BGUTIL_URL}
        ext_args["youtubepot-bgutilcli"]  = {"cli_path": BGUTIL_BIN}

    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "nocheckcertificate": True,
        "force_ipv4": True,
        "extractor_args": ext_args,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    cookie = get_cookie_file()
    if cookie:
        opts["cookiefile"] = cookie

    if extra:
        opts.update(extra)

    return opts


# ─────────────────────────────────────────
# oEmbed fallback (metadata only)
# ─────────────────────────────────────────

def oembed_info(url):
    try:
        with urllib.request.urlopen(
            f"https://www.youtube.com/oembed?url={url}&format=json", timeout=8
        ) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ─────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    step_update_ytdlp()
    step_install_deno()
    step_copy_cookies()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, step_install_bgutil_rs)
    yield


app = FastAPI(title="SHEIKH Downloader API", version="9.0.0", lifespan=lifespan)
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
    return {"status": "SHEIKH Downloader API v9.0 🚀",
            "bgutil_server": _bgutil_running,
            "bgutil_url": BGUTIL_URL}


@app.get("/check")
def check():
    import importlib.metadata as meta
    def ver(pkg):
        try: return meta.version(pkg)
        except Exception: return "unknown"

    # ping bgutil /ping endpoint
    bgutil_ping = False
    try:
        with urllib.request.urlopen(f"{BGUTIL_URL}/ping", timeout=3) as r:
            bgutil_ping = r.status == 200
    except Exception:
        bgutil_ping = os.path.exists("/tmp/bgutil.pid")

    return {
        "status": "ok",
        "yt_dlp_version": ver("yt-dlp"),
        "bgutil_plugin_py": ver("bgutil-ytdlp-pot-provider"),
        "bgutil_bin_exists": os.path.exists(BGUTIL_BIN),
        "bgutil_server_ping": bgutil_ping,
        "bgutil_global_flag": _bgutil_running,
        "deno_found": os.path.exists("/tmp/deno"),
        "secret_cookies": os.path.exists("/etc/secrets/cookies.txt"),
        "tmp_cookies": os.path.exists("/tmp/cookies.txt"),
    }


@app.get("/debug/ytdlp")
async def debug_ytdlp():
    """Verbose test — shows POT provider lines."""
    import yt_dlp
    loop = asyncio.get_event_loop()
    messages = []

    class Log:
        def debug(self, msg):
            if any(k in msg for k in ["[pot]","bgutil","PO Token","player","client"]):
                messages.append(f"DBG: {msg}")
        def warning(self, msg): messages.append(f"WRN: {msg}")
        def error(self, msg):   messages.append(f"ERR: {msg}")

    opts = make_ytdlp_opts(0)
    opts.update({"quiet": False, "verbose": True,
                 "skip_download": True, "logger": Log()})

    try:
        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=False)
        info = await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=60)
        return {"success": True, "title": info.get("title"),
                "formats": len(info.get("formats",[])), "logs": messages[-40:]}
    except Exception as e:
        return {"success": False, "error": str(e)[:500],
                "logs": messages[-40:], "bgutil_running": _bgutil_running}


@app.post("/info")
async def get_info(req: URLRequest):
    import yt_dlp
    loop = asyncio.get_event_loop()
    oembed = await loop.run_in_executor(None, oembed_info, req.url)
    last_err = ""

    for i in range(len(CLIENT_COMBOS)):
        try:
            opts = make_ytdlp_opts(i, {"skip_download": True})

            def _extract(o=opts):
                with yt_dlp.YoutubeDL(o) as ydl:
                    return ydl.extract_info(req.url, download=False)

            info = await asyncio.wait_for(
                loop.run_in_executor(None, _extract), timeout=60)

            fmts = [{"format_id": f.get("format_id"), "ext": f.get("ext"),
                     "resolution": f.get("resolution") or f.get("format_note"),
                     "filesize": f.get("filesize") or f.get("filesize_approx"),
                     "vcodec": f.get("vcodec"), "acodec": f.get("acodec"),
                     "tbr": f.get("tbr")}
                    for f in info.get("formats", [])]

            return {"source": f"yt-dlp/{CLIENT_COMBOS[i]}",
                    "title": info.get("title") or (oembed or {}).get("title",""),
                    "thumbnail": info.get("thumbnail") or (oembed or {}).get("thumbnail_url",""),
                    "duration": info.get("duration"),
                    "uploader": info.get("uploader") or (oembed or {}).get("author_name",""),
                    "view_count": info.get("view_count"),
                    "formats": fmts}

        except asyncio.TimeoutError:
            last_err = f"timeout combo {i}"; continue
        except Exception as e:
            last_err = str(e)[:200]
            print(f"⚠️  combo {i}: {last_err}"); continue

    if oembed:
        return {"source": "oembed_only", "title": oembed.get("title",""),
                "thumbnail": oembed.get("thumbnail_url",""),
                "formats": [], "warning": last_err}

    raise HTTPException(400, detail=f"All strategies failed: {last_err}")


@app.post("/download")
async def download_video(req: DownloadRequest):
    import yt_dlp
    loop = asyncio.get_event_loop()
    last_err = ""

    for i in range(len(CLIENT_COMBOS)):
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".tmp", prefix="sheikh_", dir="/tmp", delete=False
            ) as tmp:
                base = tmp.name.replace(".tmp","")

            extra = {"outtmpl": base + ".%(ext)s"}
            if req.audio_only:
                extra["format"] = "bestaudio/best"
                extra["postprocessors"] = [{"key":"FFmpegExtractAudio",
                                             "preferredcodec":"mp3","preferredquality":"192"}]
            else:
                extra["format"] = (req.format_id if req.format_id not in ["best",""]
                                   else "bestvideo*+bestaudio/best")
                extra["merge_output_format"] = "mp4"

            opts = make_ytdlp_opts(i, extra)

            def _dl(o=opts, b=base):
                with yt_dlp.YoutubeDL(o) as ydl:
                    info = ydl.extract_info(req.url, download=True)
                    fname = ydl.prepare_filename(info)
                    if not os.path.exists(fname):
                        matches = glob.glob(b + ".*")
                        fname = matches[0] if matches else fname
                    return fname

            filename = await asyncio.wait_for(
                loop.run_in_executor(None, _dl), timeout=300)

            if filename and os.path.exists(filename) and os.path.getsize(filename) > 0:
                return FileResponse(path=filename,
                                    filename=os.path.basename(filename),
                                    media_type="application/octet-stream")

        except asyncio.TimeoutError:
            last_err = f"timeout combo {i}"; continue
        except Exception as e:
            last_err = str(e)[:200]
            print(f"⚠️  dl combo {i}: {last_err}"); continue

    raise HTTPException(400, detail=f"All download strategies failed: {last_err}")
