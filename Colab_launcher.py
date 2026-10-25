# @title ⚡ Zilong Encoding Bot — Colab Launcher
# @markdown Credentials go here OR in the Colab 🔑 Secrets panel (recommended).
# @markdown Secrets override form values. Secret names = variable names below.

API_ID    = ""   # @param {type:"string"}
API_HASH  = ""   # @param {type:"string"}
BOT_TOKEN = ""   # @param {type:"string"}
OWNER_ID  = ""   # @param {type:"string"}

# @markdown ---
# @markdown ### 🔒 Repository
# @markdown `owner/repo` or a full https URL. If the repo is PRIVATE, put a
# @markdown GitHub token (read access) in Secrets as GITHUB_TOKEN, or here.
REPO_URL     = "https://github.com/ichigovrai-ai/Zilong_Encoding_bot"  # @param {type:"string"}
REPO_BRANCH  = "main"  # @param {type:"string"}
GITHUB_TOKEN = ""      # @param {type:"string"}

# @markdown ---
# @markdown ### 👥 Access control
# @markdown Space/comma-separated Telegram ids. Empty = everyone may use the bot.
AUTHORIZED_USERS = ""    # @param {type:"string"}
OWNER_ONLY       = False # @param {type:"boolean"}

# @markdown ---
# @markdown ### 🧲 Seedr (magnet links)
# @markdown Several accounts: comma-separate usernames AND passwords, same order.
SEEDR_USERNAME = ""  # @param {type:"string"}
SEEDR_PASSWORD = ""  # @param {type:"string"}
# @markdown HTTP(S) proxy only (SOCKS is not supported by the bot). Needed only
# @markdown if Seedr refuses add_torrent from Colab's IP.
SEEDR_PROXY    = ""  # @param {type:"string"}

# @markdown ---
# @markdown ### ☁️ FreeConvert (hardsub flows)
# @markdown One or more API keys, comma-separated.
FC_API_KEYS = ""  # @param {type:"string"}

# @markdown ---
# @markdown ### 📦 Dump channels
# @markdown Every uploaded file is also copied to these channels. The bot must
# @markdown be an ADMIN there. Ids like -1001234567890, separated by spaces/commas.
DUMP_CHANNELS  = ""      # @param {type:"string"}
DUMP_MODE      = "copy"  # @param ["copy", "forward"]
# @markdown Off = only YOUR uploads are dumped (other users' files stay private).
DUMP_ALL_USERS = False   # @param {type:"boolean"}

# @markdown ---
# @markdown ### ⚙️ Encoding & limits
PREFERRED_SUB_LANG       = ""        # @param {type:"string"}
DEFAULT_PRESET           = "veryfast" # @param ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"]
DEFAULT_CRF              = 23        # @param {type:"integer"}
MAX_CONCURRENT_ENCODES   = 1         # @param {type:"integer"}
MAX_CONCURRENT_DOWNLOADS = 2         # @param {type:"integer"}
MAX_CONCURRENT_UPLOADS   = 2         # @param {type:"integer"}
MAX_JOBS_PER_USER        = 5         # @param {type:"integer"}
MAX_INPUT_SIZE_MB        = 4096      # @param {type:"integer"}
MAX_OUTPUT_SIZE_MB       = 2000      # @param {type:"integer"}

# @markdown ---
# @markdown ### 🧰 Runtime options
# @markdown Keep the SQLite DB + Telegram session on Google Drive so the job
# @markdown queue survives a runtime reset. (SQLite on Drive can be slow/flaky:
# @markdown leave OFF for quick tests.)
PERSIST_ON_DRIVE = False  # @param {type:"boolean"}
# @markdown Delete the saved Telegram session (use after changing BOT_TOKEN).
RESET_SESSION    = False  # @param {type:"boolean"}
# @markdown Run the unit tests (no network) before starting the bot.
RUN_TESTS_FIRST  = False  # @param {type:"boolean"}
KEEP_ALIVE       = True   # @param {type:"boolean"}

# ─────────────────────────────────────────────────────────────────────────────
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path("/content/zilong_encoding_bot")
LOCAL_DATA_DIR = "/content/zilong_data"
DRIVE_DATA_DIR = "/content/drive/MyDrive/ZilongEncodingBot/data"
WORK_DIR = "/content/zilong_work"
DOWNLOAD_DIR = "/content/zilong_dl"
PID_FILE = Path("/tmp/zilong_encoding_bot.pid")
_PIP_MARKER = Path("/tmp/zilong_enc_pip_ok")


def _log(level: str, msg: str) -> None:
    icons = {"INFO": "ℹ️", "OK": "✅", "WARN": "⚠️", "ERR": "❌", "STEP": "🔧"}
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {icons.get(level, '')} {msg}", flush=True)


def _secret(name: str, default: str = "") -> str:
    """Colab Secrets > environment > form value."""
    try:
        from google.colab import userdata  # type: ignore
        val = userdata.get(name)
        if val is not None and str(val).strip():
            return str(val).strip()
    except Exception:
        pass
    return (os.environ.get(name) or str(default or "")).strip()


def _secret_int(name: str, default: int, lo: int, hi: int) -> str:
    try:
        val = int(_secret(name, str(default)) or default)
    except ValueError:
        val = default
    return str(max(lo, min(hi, val)))


def _mask(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _check_python() -> None:
    if sys.version_info < (3, 12):
        raise SystemExit(
            f"Python {sys.version_info.major}.{sys.version_info.minor} detected — "
            "the bot requires Python >= 3.12. Use a Colab runtime with Python 3.12+."
        )
    _log("OK", f"Python {sys.version.split()[0]}")


def _mount_drive() -> None:
    try:
        from google.colab import drive  # type: ignore
        _log("STEP", "Mounting Google Drive for persistent bot data…")
        drive.mount("/content/drive", force_remount=False)
        _log("OK", "Google Drive mounted")
    except Exception as exc:
        raise SystemExit(f"Drive mount failed ({exc}). Set PERSIST_ON_DRIVE=False or retry.")


def _github_token() -> str:
    return _secret("GITHUB_TOKEN", GITHUB_TOKEN) or _secret("GH_TOKEN")


def _clean_repo_url() -> str:
    raw = _secret("REPO_URL", REPO_URL)
    if not raw:
        raise SystemExit("REPO_URL is required (e.g. 'your-user/Zilong_Encoding_bot').")
    if not raw.startswith(("http://", "https://")):
        raw = f"https://github.com/{raw.strip('/')}"
    if not raw.endswith(".git"):
        raw += ".git"
    return raw


def _clone_or_update_repo() -> Path:
    clean_url = _clean_repo_url()
    branch = _secret("REPO_BRANCH", REPO_BRANCH) or "main"
    token = _github_token()
    url = clean_url.replace("https://", f"https://x-access-token:{token}@", 1) if token else clean_url

    if (BASE_DIR / ".git").exists():
        _log("STEP", f"Updating repository ({branch})…")
        run = lambda *a: subprocess.run(["git", "-C", str(BASE_DIR), *a],
                                        check=False, capture_output=True, text=True)
        run("remote", "set-url", "origin", url)
        fetched = run("fetch", "--depth=1", "origin", branch)
        if fetched.returncode == 0:
            run("checkout", "-B", branch, "FETCH_HEAD")
            run("reset", "--hard", "FETCH_HEAD")
        else:
            _log("WARN", f"git fetch failed: {_mask((fetched.stderr or '').strip(), token)[-300:]}")
        run("remote", "set-url", "origin", clean_url)  # never keep the token on disk
        return BASE_DIR

    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR)
    _log("STEP", "Cloning repository…")
    result = subprocess.run(
        ["git", "clone", "--depth=1", "--branch", branch, url, str(BASE_DIR)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        _log("ERR", f"git clone failed: {_mask((result.stderr or '').strip(), token)[-400:]}")
        if not token:
            _log("ERR", "If the repo is PRIVATE, add a GitHub token (read access) "
                        "in Colab 🔑 Secrets as GITHUB_TOKEN, then re-run.")
        else:
            _log("ERR", "Token rejected or no access to this repo / branch — check "
                        "the token scope, expiry, REPO_URL and REPO_BRANCH.")
        raise SystemExit("Repository clone failed — see the messages above.")
    subprocess.run(["git", "-C", str(BASE_DIR), "remote", "set-url", "origin", clean_url],
                   check=False, capture_output=True)
    return BASE_DIR


def _install_system_packages() -> None:
    _log("STEP", "Installing system packages (ffmpeg, mediainfo, aria2, fonts)…")
    subprocess.run(
        "apt-get update -qq && apt-get install -y -qq "
        "ffmpeg mediainfo aria2 fonts-dejavu-core fonts-noto-cjk 2>/dev/null",
        shell=True, check=False,
    )
    for binary in ("ffmpeg", "ffprobe", "mediainfo", "aria2c"):
        if shutil.which(binary) is None:
            _log("WARN", f"{binary} not found after install")
    # Hardsubbing needs the libass-backed `subtitles` filter.
    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                             capture_output=True, text=True).stdout or ""
    if " subtitles " in filters:
        _log("OK", "FFmpeg has the 'subtitles' filter (libass) — hardsub OK")
    else:
        _log("WARN", "FFmpeg has NO 'subtitles' filter (libass): hardsub will fail")
    _log("OK", "System packages ready")


def _requirements_hash(repo: Path) -> str:
    try:
        return hashlib.md5((repo / "requirements.txt").read_bytes()).hexdigest()
    except Exception:
        return ""


def _pinned_pyrofork(repo: Path) -> str:
    try:
        m = re.search(r"^pyrofork==([\w.]+)", (repo / "requirements.txt").read_text(), re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _key_packages_present(repo: Path) -> bool:
    try:
        import aiohttp, aiosqlite, dotenv  # noqa: F401
        import pyrogram  # noqa: F401  (provided by pyrofork)
        want = _pinned_pyrofork(repo)
        return not want or getattr(pyrogram, "__version__", "") == want
    except Exception:
        return False


def _install_python_packages(repo: Path, dev: bool = False) -> None:
    want = _requirements_hash(repo)
    if (not dev and want and _PIP_MARKER.exists()
            and _PIP_MARKER.read_text(encoding="utf-8").strip() == want
            and _key_packages_present(repo)):
        _log("OK", "Requirements unchanged and packages present — skipping install")
        return

    _log("STEP", "Installing Python packages…")
    # pyrofork provides the `pyrogram` module; upstream pyrogram would clash.
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-q", "-y", "pyrogram"], check=False)
    req = str(repo / ("requirements-dev.txt" if dev else "requirements.txt"))
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "uv"], check=False)
        subprocess.check_call([sys.executable, "-m", "uv", "pip", "install", "--system", "-r", req])
        _log("OK", "Python packages installed (uv)")
    except Exception as exc:
        _log("WARN", f"uv install failed ({exc}); falling back to pip")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", req])
        _log("OK", "Python packages installed")
    if want and not dev:
        try:
            _PIP_MARKER.write_text(want, encoding="utf-8")
        except Exception:
            pass


def _stop_previous_bot() -> None:
    """Kill a bot left over from a previous run of this cell (two instances with
    the same token would fight over updates)."""
    if not PID_FILE.exists():
        return
    try:
        old = int(PID_FILE.read_text(encoding="utf-8").strip())
        cmd = Path(f"/proc/{old}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
        if "-m bot" in cmd:
            os.kill(old, signal.SIGTERM)
            time.sleep(3)
            _log("OK", f"Stopped previous bot process PID {old}")
    except Exception:
        pass
    PID_FILE.unlink(missing_ok=True)


def _normalize_proxy(value: str) -> str:
    """'host:port' or 'user:pass@host:port' -> 'http://…' (the bot only accepts http/https)."""
    value = value.strip()
    if value and "://" not in value:
        return "http://" + value
    return value


def _collect_env() -> dict[str, str]:
    api_id = _secret("API_ID", API_ID)
    owner_id = _secret("OWNER_ID", OWNER_ID)
    data_dir = DRIVE_DATA_DIR if (PERSIST_ON_DRIVE or _secret("PERSIST_ON_DRIVE") == "1") else LOCAL_DATA_DIR
    for d in (data_dir, WORK_DIR, DOWNLOAD_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)

    owner_only = OWNER_ONLY or _secret("OWNER_ONLY") in ("1", "true", "True")
    env = {
        "API_ID": api_id,
        "API_HASH": _secret("API_HASH", API_HASH),
        "BOT_TOKEN": _secret("BOT_TOKEN", BOT_TOKEN),
        "OWNER_ID": owner_id,
        "AUTHORIZED_USERS": _secret("AUTHORIZED_USERS", AUTHORIZED_USERS),
        "OWNER_ONLY": "1" if owner_only else "0",
        "SUBSCRIPTION_MODE": _secret("SUBSCRIPTION_MODE", "0"),
        "SEEDR_USERNAME": _secret("SEEDR_USERNAME", SEEDR_USERNAME),
        "SEEDR_PASSWORD": _secret("SEEDR_PASSWORD", SEEDR_PASSWORD),
        "SEEDR_PROXY": _normalize_proxy(_secret("SEEDR_PROXY", SEEDR_PROXY)),
        "FC_API_KEYS": _secret("FC_API_KEYS", FC_API_KEYS),
        "DUMP_CHANNELS": _secret("DUMP_CHANNELS", DUMP_CHANNELS),
        "DUMP_MODE": _secret("DUMP_MODE", DUMP_MODE),
        "DUMP_ALL_USERS": "1" if (DUMP_ALL_USERS or _secret("DUMP_ALL_USERS") in ("1", "true", "True")) else "0",
        "PREFERRED_SUB_LANG": _secret("PREFERRED_SUB_LANG", PREFERRED_SUB_LANG),
        "DEFAULT_PRESET": _secret("DEFAULT_PRESET", DEFAULT_PRESET),
        "DEFAULT_CRF": _secret_int("DEFAULT_CRF", DEFAULT_CRF, 0, 51),
        "MAX_CONCURRENT_ENCODES": _secret_int("MAX_CONCURRENT_ENCODES", MAX_CONCURRENT_ENCODES, 1, 8),
        "MAX_CONCURRENT_DOWNLOADS": _secret_int("MAX_CONCURRENT_DOWNLOADS", MAX_CONCURRENT_DOWNLOADS, 1, 8),
        "MAX_CONCURRENT_UPLOADS": _secret_int("MAX_CONCURRENT_UPLOADS", MAX_CONCURRENT_UPLOADS, 1, 8),
        "MAX_JOBS_PER_USER": _secret_int("MAX_JOBS_PER_USER", MAX_JOBS_PER_USER, 1, 100),
        "MAX_INPUT_SIZE_MB": _secret_int("MAX_INPUT_SIZE_MB", MAX_INPUT_SIZE_MB, 16, 64 * 1024),
        "MAX_OUTPUT_SIZE_MB": _secret_int("MAX_OUTPUT_SIZE_MB", MAX_OUTPUT_SIZE_MB, 16, 64 * 1024),
        # Paths (absolute so they don't depend on the working directory)
        "DATABASE_PATH": f"{data_dir}/bot.db",
        "SESSION_DIR": data_dir,
        "WORK_DIR": WORK_DIR,
        "DOWNLOAD_DIR": DOWNLOAD_DIR,
        "PYTHONUNBUFFERED": "1",
    }
    # Optional passthroughs (only set if present in Secrets)
    for opt in ("SEEDR_AUTO_DELETE", "SEEDR_POLL_INTERVAL", "SEEDR_TIMEOUT_HOURS",
                "MIN_FREE_DISK_GB", "JOB_TIMEOUT_HOURS", "JOB_RETENTION_HOURS",
                "DEFAULT_VIDEO_CODEC", "DEFAULT_AUDIO_CODEC", "DEFAULT_RESOLUTION",
                "FALLBACK_FONT", "PROGRESS_UPDATE_INTERVAL"):
        val = _secret(opt)
        if val:
            env[opt] = val
    return env


def _validate(env: dict[str, str]) -> None:
    problems = []
    if not env["API_ID"].isdigit() or env["API_ID"] == "0":
        problems.append("API_ID must be a number (https://my.telegram.org/apps)")
    if not env["API_HASH"]:
        problems.append("API_HASH is required")
    if ":" not in env["BOT_TOKEN"]:
        problems.append("BOT_TOKEN is missing or malformed (expected 123456:ABC…)")
    if not env["OWNER_ID"].lstrip("-").isdigit() or env["OWNER_ID"] == "0":
        problems.append("OWNER_ID must be your numeric Telegram id (@userinfobot)")
    seedr_users = [u for u in env["SEEDR_USERNAME"].split(",") if u.strip()]
    seedr_pwds = [p for p in env["SEEDR_PASSWORD"].split(",") if p.strip()]
    if bool(seedr_users) != bool(seedr_pwds) or len(seedr_users) != len(seedr_pwds):
        problems.append("SEEDR_USERNAME / SEEDR_PASSWORD must be set together, same count")
    if env["SEEDR_PROXY"] and not env["SEEDR_PROXY"].startswith(("http://", "https://")):
        scheme = env["SEEDR_PROXY"].split("://", 1)[0]
        problems.append(f"SEEDR_PROXY uses '{scheme}://' but the bot only supports http(s):// "
                        "proxies. Use an HTTP proxy, or empty the SEEDR_PROXY secret/field.")
    for ch in env["DUMP_CHANNELS"].replace(",", " ").split():
        if not ch.lstrip("-").isdigit() or not ch.startswith("-"):
            problems.append(f"DUMP_CHANNELS: '{ch}' is not a channel id "
                            "(expected something like -1001234567890)")
    if problems:
        for p in problems:
            print(f"  ❌ {p}")
        raise SystemExit("Invalid configuration. Fix the items above (form or Colab Secrets).")


def _reset_session(env: dict[str, str]) -> None:
    if not (RESET_SESSION or _secret("RESET_SESSION") == "1"):
        return
    for f in Path(env["SESSION_DIR"]).glob("encoding_bot.session*"):
        try:
            f.unlink()
            _log("OK", f"Removed session file {f.name}")
        except OSError:
            pass


def _keep_alive() -> None:
    try:
        from IPython.display import Javascript, display
        display(Javascript("""
        setInterval(function(){
            document.querySelector("#top-toolbar .colab-connect-button")?.click();
            document.querySelector("colab-connect-button")?.shadowRoot?.querySelector("#connect")?.click();
            document.querySelector("#ok")?.click();
        }, 60000);
        """))
        _log("OK", "JS keep-alive injected")
    except Exception:
        _log("WARN", "JS keep-alive skipped")
    threading.Thread(
        target=lambda: [time.sleep(300) or print(f"\r[{datetime.now().strftime('%H:%M')}] 💓", end="", flush=True)
                        for _ in iter(int, 1)],
        daemon=True,
    ).start()


def _print_summary(env: dict[str, str]) -> None:
    _log("OK", f"OWNER_ID={env['OWNER_ID']}  owner_only={env['OWNER_ONLY']}")
    n_seedr = len([u for u in env["SEEDR_USERNAME"].split(",") if u.strip()])
    n_fc = len([k for k in env["FC_API_KEYS"].split(",") if k.strip()])
    _log("OK" if n_seedr else "INFO", f"Seedr accounts: {n_seedr}")
    _log("OK" if n_fc else "INFO", f"FreeConvert keys: {n_fc}"
         + ("" if n_fc else " (☁️ FC hardsub flows disabled)"))
    n_dump = len(env["DUMP_CHANNELS"].replace(",", " ").split())
    if n_dump:
        scope = "everyone's uploads" if env["DUMP_ALL_USERS"] == "1" else "your uploads only"
        _log("OK", f"Dump channels: {n_dump} ({env['DUMP_MODE']}, {scope})")
    _log("OK", f"Data dir: {env['SESSION_DIR']}")
    if n_seedr and not env["SEEDR_PROXY"]:
        _log("WARN", "SEEDR_PROXY is empty. If magnet-adds are refused, Seedr is "
                     "probably blocking Colab's IP: add an HTTP(S) proxy.")


def _run_tests(repo: Path) -> None:
    _log("STEP", "Running unit tests…")
    _install_python_packages(repo, dev=True)
    result = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=str(repo))
    _log("OK" if result.returncode == 0 else "WARN",
         "Tests passed" if result.returncode == 0 else f"Tests failed (exit {result.returncode})")


def _run_bot(repo: Path, env: dict[str, str]) -> None:
    # Secrets are passed through the process environment only (no .env file on disk).
    stale_env = repo / ".env"
    if stale_env.exists():
        stale_env.unlink()  # Config.load() uses override=True: a stale .env would win
        _log("WARN", "Removed a stale .env from the repo directory")

    bot_env = {**os.environ, **env}
    max_restarts = int(_secret_int("ZILONG_MAX_RESTARTS", 50, 1, 500))
    restart_count = 0
    flood_re = re.compile(r"(?:FLOOD_WAIT_SECONDS=(\d+)|A wait of (\d+) seconds is required|FloodWait (\d+)s)")
    proc = None

    _log("OK", "Starting bot…\n" + "─" * 50)
    try:
        while restart_count < max_restarts:
            start = time.time()
            proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "bot"],
                cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=bot_env,
            )
            PID_FILE.write_text(str(proc.pid), encoding="utf-8")
            captured: list[str] = []
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="", flush=True)
                captured.append(line)
                if len(captured) > 200:
                    del captured[:100]
            code = proc.wait()
            PID_FILE.unlink(missing_ok=True)
            if code == 0:
                _log("OK", "Bot stopped cleanly")
                return
            if any("Configuration invalid" in l for l in captured):
                _log("ERR", "Bot refused to start: invalid configuration (see above). Not restarting.")
                return
            if time.time() - start > 300:
                restart_count = 0
            restart_count += 1
            wait = min(5 * restart_count, 30)
            for line in reversed(captured):
                m = flood_re.search(line)
                if m:
                    wait = int(next(g for g in m.groups() if g)) + 5
                    break
            _log("WARN", f"Bot exited code={code}. Restarting in {wait}s [{restart_count}/{max_restarts}]")
            time.sleep(wait)
        _log("ERR", "Too many restarts. Launcher stopped.")
    finally:
        # Cell stopped (■ button) or launcher exiting: shut the bot down gracefully
        # so no orphan keeps polling with the same token.
        if proc is not None and proc.poll() is None:
            _log("STEP", "Stopping bot (SIGTERM)…")
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            PID_FILE.unlink(missing_ok=True)


print("⚡ Zilong Encoding Bot — Colab Launcher")
print("─" * 50)
_check_python()
if PERSIST_ON_DRIVE or _secret("PERSIST_ON_DRIVE") == "1":
    _mount_drive()
repo = _clone_or_update_repo()
_install_system_packages()
_install_python_packages(repo)
_stop_previous_bot()
env = _collect_env()
_validate(env)
_reset_session(env)
if RUN_TESTS_FIRST or _secret("RUN_TESTS_FIRST") == "1":
    _run_tests(repo)
if KEEP_ALIVE:
    _keep_alive()
_print_summary(env)
_run_bot(repo, env)
