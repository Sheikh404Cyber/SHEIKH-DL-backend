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
BGUTIL_PORT = 4416
BGUTIL_URL  = f"http://127.0.0.1:{BGUTIL_PORT}"
_bgutil_running = False


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

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
# STARTUP STEPS
# ─────────────────────────────────────────

def step_update_ytdlp():
    print("⏳ [1/5] Updating yt-dlp nightly + bgutil plugin...")
    code, _, err = run_cmd([
        sys.executable, "-m", "pip", "install", "-U", "--pre",
        "yt-dlp[default]", "bgutil-ytdlp-pot-provider"
    ], timeout=180)
    print("✅ yt-dlp + bgutil plugin updated" if code == 0
          else f"⚠️  pip warn: {err[-100:]}")


def step_install_node22():
    """Install Node.js 22 — required by bgutil 2.0.0"""
    global _node_bin

    # Check if already good enough
    node = shutil.which("node") or "/tmp/node"
    if os.path.exists(node):
        code, ver, _ = run_cmd([node, "--version"])
        if code == 0:
            major = int(ver.strip().lstrip("v").split(".")[0])
            if major >= 22:
                print(f"✅ [2/5] Node.js {ver.strip()} already installed")
                os.environ["PATH"] = os.path.dirname(node) + ":" + os.environ.get("PATH","")
                return node
            else:
                print(f"⚠️  Node.js {ver.strip()} too old, need >=22, reinstalling...")

    print("⏳ [2/5] Installing Node.js 22...")
    arch = platform.machine().lower()
    if "aarch64" in arch or "arm64" in arch:
        url  = "https://nodejs.org/dist/v22.11.0/node-v22.11.0-linux-arm64.tar.gz"
        dname = "node-v22.11.0-linux-arm64"
    else:
        url  = "https://nodejs.org/dist/v22.11.0/node-v22.11.0-linux-x64.tar.gz"
        dname = "node-v22.11.0-linux-x64"

    try:
        import tarfile
        tar_path = "/tmp/node22.tar.gz"
        print(f"  Downloading Node.js 22 from {url}...")
        urllib.request.urlretrieve(url, tar_path)
        extract_dir = "/tmp/node22_install"
        os.makedirs(extract_dir, exist_ok=True)
        with tarfile.open(tar_path, "r:gz") as t:
            t.extractall(extract_dir)
        node_bin_dir = f"{extract_dir}/{dname}/bin"
        os.environ["PATH"] = node_bin_dir + ":" + os.environ.get("PATH","")
        # Symlink to /tmp for convenience
        for b in ["node","npm","npx"]:
            src = os.path.join(node_bin_dir, b)
            dst = f"/tmp/{b}"
            if os.path.exists(src):
                try:
                    if os.path.exists(dst): os.remove(dst)
                    os.symlink(src, dst)
                except Exception:
                    shutil.copy2(src, dst)
                    os.chmod(dst, 0o755)
        node_path = shutil.which("node") or "/tmp/node"
        code, ver, _ = run_cmd([node_path, "--version"])
        print(f"✅ Node.js installed: {ver.strip()}")
        return node_path
    except Exception as e:
        print(f"⚠️  Node.js 22 install failed: {e}")
        return None


def step_install_deno():
    deno = "/tmp/deno"
    if os.path.exists(deno):
        os.environ["PATH"] = "/tmp:" + os.environ.get("PATH","")
        print("✅ [3/5] Deno already installed")
        return
    print("⏳ [3/5] Installing Deno...")
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
        os.environ["PATH"] = "/tmp:" + os.environ.get("PATH","")
        print("✅ Deno installed at /tmp/deno")
    except Exception as e:
        print(f"⚠️  Deno install failed: {e}")


def step_setup_bgutil(node_bin):
    """Clone bgutil repo, build it, start HTTP server on port 4416."""
    global _bgutil_running
    pid_file   = "/tmp/bgutil.pid"
    server_dir = "/tmp/bgutil-server"
    srv_src    = os.path.join(server_dir, "server")
    build_main = os.path.join(srv_src, "build", "main.js")

    if not node_bin or not os.path.exists(node_bin):
        node_bin = shutil.which("node") or "/tmp/node"
    if not os.path.exists(node_bin):
        print("⚠️  [4/5] No Node.js — bgutil server skipped")
        return

    # Check already running
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            _bgutil_running = True
            print(f"✅ [4/5] bgutil server already running PID={pid}")
            return
        except Exception:
            pass

    print("⏳ [4/5] Setting up bgutil POT server...")

    # Clone
    if not os.path.exists(server_dir):
        code, _, err = run_cmd([
            "git", "clone", "--depth=1", "--branch", "2.0.0",
            "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git",
            server_dir
        ], timeout=120)
        if code != 0:
            print(f"⚠️  bgutil clone failed: {err[-150:]}")
            return

    npm_bin = shutil.which("npm") or "/tmp/npm"
    npx_bin = shutil.which("npx") or "/tmp/npx"

    # npm ci
    if not os.path.exists(os.path.join(srv_src, "node_modules")):
        print("  ⏳ npm ci...")
        code, out, err = run_cmd([npm_bin, "ci"], timeout=240, cwd=srv_src)
        if code != 0:
            print(f"  ⚠️  npm ci failed, trying npm install: {err[-100:]}")
            run_cmd([npm_bin, "install"], timeout=240, cwd=srv_src)

    # tsc compile
    if not os.path.exists(build_main):
        print("  ⏳ npx tsc (TypeScript compile)...")
        code, out, err = run_cmd([npx_bin, "tsc"], timeout=120, cwd=srv_src)
        if code != 0:
            print(f"  ⚠️  tsc failed: {err[-200:]}")

    if not os.path.exists(build_main):
        print("⚠️  bgutil build/main.js not found — server skipped")
        return

    # Start server
    proc = subprocess.Popen(
        [node_bin, build_main, "--port", str(BGUTIL_PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=srv_src
    )
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))

    # Wait up to 10s for server to be ready
    for _ in range(10):
        time.sleep(1)
        try:
            urllib.request.urlopen(f"{BGUTIL_URL}/", timeout=2)
            break
        except Exception:
            pass

    _bgutil_running = True
    print(f"✅ bgutil POT server started — PID={proc.pid} on port {BGUTIL_PORT}")


def step_copy_cookies():
    print("⏳ [5/5] Setting up cookies...")
    src, dst = "/etc/secrets/cookies.txt", "/tmp/cookies.txt"
    if os.path.exists(src) and not os.path.exists(dst):
        shutil.copy2(src, dst)
        print("✅ Cookies copied to /tmp/cookies.txt")
    elif os.path.exists(dst):
        print("✅ Cookies already at /tmp/cookies.txt")
    else:
        print("ℹ️  No cookies (optional)")


# ─────────────────────────────────────────
# YT-DLP OPTIONS
# ─────────────────────────────────────────

# Best client combos for datacenter IPs — ordered by success rate
CLIENT_COMBOS = [
    ["tv_downgraded", "web_embedded"],
    ["android_vr"],
    ["tv_downgraded"],
    ["web_embedded"],
    ["mweb"],
]


def make_ytdlp_opts(combo_index=0, extra=None):
    clients = CLIENT_COMBOS[combo_index % len(CLIENT_COMBOS)]

    # Build extractor_args — include bgutil base_url explicitly
    ext_args = {
        "youtube": {
            "player_client": clients,
            "player_skip": ["webpage"],
        },
    }
    if _bgutil_running:
        ext_args["youtubepot-bgutilhttp"] = {
            "base_url": BGUTIL_URL,
        }

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
# OEMBED FALLBACK
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
    node_bin = step_install_node22()
    step_install_deno()
    step_copy_cookies()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, step_setup_bgutil, node_bin)
    yield


app = FastAPI(title="SHEIKH Downloader API", version="8.0.0", lifespan=lifespan)
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
    return {"status": "SHEIKH Downloader API v8.0 🚀",
            "bgutil_server": _bgutil_running}


@app.get("/check")
def check():
    import importlib.metadata as meta
    def ver(pkg):
        try: return meta.version(pkg)
        except Exception: return "unknown"

    # ping bgutil
    bgutil_ok = False
    try:
        urllib.request.urlopen(f"{BGUTIL_URL}/", timeout=2)
        bgutil_ok = True
    except Exception:
        bgutil_ok = os.path.exists("/tmp/bgutil.pid")

    node = shutil.which("node") or "/tmp/node"
    node_ver = ""
    if os.path.exists(node):
        _, nv, _ = run_cmd([node, "--version"])
        node_ver = nv.strip()

    return {
        "status": "ok",
        "yt_dlp_version": ver("yt-dlp"),
        "bgutil_plugin": ver("bgutil-ytdlp-pot-provider"),
        "bgutil_server": bgutil_ok,
        "bgutil_url": BGUTIL_URL,
        "node_version": node_ver,
        "deno_found": os.path.exists("/tmp/deno"),
        "secret_cookies": os.path.exists("/etc/secrets/cookies.txt"),
        "tmp_cookies": os.path.exists("/tmp/cookies.txt"),
        "bgutil_global": _bgutil_running,
    }


@app.get("/debug/ytdlp")
async def debug_ytdlp():
    """Verbose yt-dlp test — shows exact error + bgutil POT logs."""
    import yt_dlp
    loop = asyncio.get_event_loop()

    messages = []

    class LogCollector:
        def debug(self, msg):
            if "[pot]" in msg or "bgutil" in msg or "PO Token" in msg or "player" in msg.lower():
                messages.append(f"[DEBUG] {msg}")
        def warning(self, msg): messages.append(f"[WARN] {msg}")
        def error(self, msg): messages.append(f"[ERROR] {msg}")

    opts = make_ytdlp_opts(0)
    opts["quiet"] = False
    opts["verbose"] = True
    opts["skip_download"] = True
    opts["logger"] = LogCollector()

    try:
        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    download=False
                )
        info = await asyncio.wait_for(
            loop.run_in_executor(None, _run), timeout=60
        )
        return {
            "success": True,
            "title": info.get("title"),
            "formats_count": len(info.get("formats", [])),
            "log_lines": messages[-30:],
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)[:500],
            "log_lines": messages[-30:],
            "bgutil_running": _bgutil_running,
        }


@app.post("/info")
async def get_info(req: URLRequest):
    import yt_dlp
    loop  = asyncio.get_event_loop()
    oembed = await loop.run_in_executor(None, oembed_info, req.url)
    last_err = ""

    for i in range(len(CLIENT_COMBOS)):
        try:
            opts = make_ytdlp_opts(i, {"skip_download": True})

            def _extract(o=opts):
                with yt_dlp.YoutubeDL(o) as ydl:
                    return ydl.extract_info(req.url, download=False)

            info = await asyncio.wait_for(
                loop.run_in_executor(None, _extract), timeout=60
            )

            fmts = []
            for f in info.get("formats", []):
                fmts.append({
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "resolution": f.get("resolution") or f.get("format_note"),
                    "filesize": f.get("filesize") or f.get("filesize_approx"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                    "tbr": f.get("tbr"),
                })

            return {
                "source": f"yt-dlp/{CLIENT_COMBOS[i]}",
                "title": info.get("title") or (oembed or {}).get("title",""),
                "thumbnail": info.get("thumbnail") or (oembed or {}).get("thumbnail_url",""),
                "duration": info.get("duration"),
                "uploader": info.get("uploader") or (oembed or {}).get("author_name",""),
                "view_count": info.get("view_count"),
                "formats": fmts,
            }
        except asyncio.TimeoutError:
            last_err = f"timeout on combo {i}"
            continue
        except Exception as e:
            last_err = str(e)[:200]
            print(f"⚠️  combo {i} {CLIENT_COMBOS[i]}: {last_err}")
            continue

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

            out_tmpl = base + ".%(ext)s"

            extra = {}
            if req.audio_only:
                extra["format"] = "bestaudio/best"
                extra["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }]
            else:
                extra["format"] = (req.format_id if req.format_id not in ["best",""]
                                   else "bestvideo*+bestaudio/best")
                extra["merge_output_format"] = "mp4"

            extra["outtmpl"] = out_tmpl
            opts = make_ytdlp_opts(i, extra)

            def _dl(o=opts):
                with yt_dlp.YoutubeDL(o) as ydl:
                    info = ydl.extract_info(req.url, download=True)
                    return ydl.prepare_filename(info)

            filename = await asyncio.wait_for(
                loop.run_in_executor(None, _dl), timeout=300
            )

            if not os.path.exists(filename):
                matches = glob.glob(base + ".*")
                filename = matches[0] if matches else None

            if filename and os.path.exists(filename) and os.path.getsize(filename) > 0:
                return FileResponse(path=filename,
                                    filename=os.path.basename(filename),
                                    media_type="application/octet-stream")
        except asyncio.TimeoutError:
            last_err = f"timeout combo {i}"
            continue
        except Exception as e:
            last_err = str(e)[:200]
            print(f"⚠️  dl combo {i}: {last_err}")
            continue

    raise HTTPException(400, detail=f"All download strategies failed: {last_err}")
