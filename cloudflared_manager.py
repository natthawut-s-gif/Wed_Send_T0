import json
import os
import re
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
CLOUDFLARED_DIR = BASE_DIR / "cloudflared"
TUNNEL_SETTINGS_FILE = BASE_DIR / "cloudflared-settings.json"
TUNNEL_CONFIG_FILE = CLOUDFLARED_DIR / "config.yml"
TUNNEL_LOG_FILE = BASE_DIR / ".cloudflared.log"
TUNNEL_PID_FILE = BASE_DIR / ".cloudflared.pid"
QUICK_TUNNEL_LOG_FILE = BASE_DIR / ".cloudflared-quick.log"
QUICK_TUNNEL_PID_FILE = BASE_DIR / ".cloudflared-quick.pid"

DEFAULT_PORT = 3000
DEFAULT_HOSTNAME = "doc.yourdomain.com"
DEFAULT_TUNNEL_NAME = "doc-extraction-tunnel"
DEFAULT_TUNNEL_ID = "YOUR-TUNNEL-UUID"
DEFAULT_METRICS_ADDRESS = "127.0.0.1:20241"
DEFAULT_LOG_LEVEL = "info"
DEFAULT_PROTOCOL = "auto"
DEFAULT_QUICK_TUNNEL_WAIT_SECONDS = 600
QUICK_TUNNEL_URL_PATTERN = re.compile(r"https://[-a-zA-Z0-9.]*trycloudflare\.com")


def _read_env_value(name: str, default: str) -> str:
    if not ENV_FILE.exists():
        return default

    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            cleaned = value.strip()
            return cleaned or default

    return default


def read_env_port() -> int:
    try:
        return int(_read_env_value("PORT", str(DEFAULT_PORT)))
    except ValueError:
        return DEFAULT_PORT


def read_quick_tunnel_wait_seconds() -> int:
    try:
        value = int(_read_env_value("QUICK_TUNNEL_WAIT_SECONDS", str(DEFAULT_QUICK_TUNNEL_WAIT_SECONDS)))
    except ValueError:
        return DEFAULT_QUICK_TUNNEL_WAIT_SECONDS

    return max(30, value)


def default_service_url() -> str:
    return f"http://localhost:{read_env_port()}"


def default_credentials_file() -> str:
    return str(Path.home() / ".cloudflared" / f"{DEFAULT_TUNNEL_ID}.json")


def default_settings() -> dict:
    return {
        "tunnel_name": DEFAULT_TUNNEL_NAME,
        "tunnel_id": DEFAULT_TUNNEL_ID,
        "hostname": DEFAULT_HOSTNAME,
        "service_url": default_service_url(),
        "credentials_file": default_credentials_file(),
        "metrics_address": DEFAULT_METRICS_ADDRESS,
        "log_level": DEFAULT_LOG_LEVEL,
        "protocol": DEFAULT_PROTOCOL,
        "executable_path": "",
    }


def normalize_settings(payload: dict | None) -> dict:
    defaults = default_settings()
    payload = payload or {}
    normalized = {}

    for key, default_value in defaults.items():
        value = payload.get(key, default_value)
        if isinstance(default_value, str):
            normalized[key] = str(value).strip() if value is not None else default_value
        else:
            normalized[key] = value

    if not normalized["service_url"]:
        normalized["service_url"] = default_service_url()
    if not normalized["metrics_address"]:
        normalized["metrics_address"] = DEFAULT_METRICS_ADDRESS
    if not normalized["log_level"]:
        normalized["log_level"] = DEFAULT_LOG_LEVEL
    if not normalized["protocol"]:
        normalized["protocol"] = DEFAULT_PROTOCOL

    return normalized


def render_config(settings: dict) -> str:
    return "\n".join(
        [
            "# Generated for Cloudflare Tunnel local management",
            "# Update cloudflared-settings.json or use manage_web_ui.py to edit these values.",
            f"tunnel: '{settings['tunnel_id']}'",
            f"credentials-file: '{settings['credentials_file']}'",
            "",
            "ingress:",
            f"  - hostname: '{settings['hostname']}'",
            f"    service: '{settings['service_url']}'",
            "  - service: http_status:404",
            "",
        ]
    )


def write_config(settings: dict | None = None) -> Path:
    settings = normalize_settings(settings or load_settings())
    CLOUDFLARED_DIR.mkdir(parents=True, exist_ok=True)
    TUNNEL_CONFIG_FILE.write_text(render_config(settings), encoding="utf-8")
    return TUNNEL_CONFIG_FILE


def load_settings() -> dict:
    if not TUNNEL_SETTINGS_FILE.exists():
        settings = default_settings()
        save_settings(settings)
        return settings

    try:
        payload = json.loads(TUNNEL_SETTINGS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = default_settings()

    settings = normalize_settings(payload)
    write_config(settings)
    return settings


def save_settings(payload: dict) -> dict:
    settings = normalize_settings(payload)
    TUNNEL_SETTINGS_FILE.write_text(
        f"{json.dumps(settings, indent=2, ensure_ascii=False)}\n",
        encoding="utf-8",
    )
    write_config(settings)
    return settings


def resolve_executable(settings: dict | None = None) -> str | None:
    settings = normalize_settings(settings or load_settings())
    candidates = []

    if settings["executable_path"]:
        candidates.append(settings["executable_path"])

    which_path = shutil.which("cloudflared")
    if which_path:
        candidates.append(which_path)

    candidates.extend(
        [
            r"C:\Program Files\Cloudflare\Cloudflared\cloudflared.exe",
            r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
            r"C:\Cloudflared\bin\cloudflared.exe",
        ]
    )

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))

    return None


def read_pid() -> int | None:
    return _read_pid_file(TUNNEL_PID_FILE)


def _read_pid_file(pid_file: Path) -> int | None:
    if not pid_file.exists():
        return None

    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def write_pid(pid: int) -> None:
    _write_pid_file(TUNNEL_PID_FILE, pid)


def _write_pid_file(pid_file: Path, pid: int) -> None:
    pid_file.write_text(str(pid), encoding="utf-8")


def clear_pid() -> None:
    _clear_pid_file(TUNNEL_PID_FILE)


def _clear_pid_file(pid_file: Path) -> None:
    if pid_file.exists():
        pid_file.unlink()


def process_is_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False

    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def public_url(settings: dict | None = None) -> str:
    settings = normalize_settings(settings or load_settings())
    return f"https://{settings['hostname']}"


def metrics_url(settings: dict | None = None) -> str:
    settings = normalize_settings(settings or load_settings())
    return f"http://{settings['metrics_address']}/metrics"


def read_log_tail(max_lines: int = 120) -> str:
    return read_named_tunnel_log_tail(max_lines=max_lines)


def read_named_tunnel_log_tail(max_lines: int = 120) -> str:
    return _read_log_tail(TUNNEL_LOG_FILE, max_lines=max_lines)


def read_quick_tunnel_log_tail(max_lines: int = 120) -> str:
    return _read_log_tail(QUICK_TUNNEL_LOG_FILE, max_lines=max_lines)


def _read_log_tail(log_file: Path, max_lines: int = 120) -> str:
    if not log_file.exists():
        return ""

    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def read_quick_tunnel_public_url() -> str:
    if not QUICK_TUNNEL_LOG_FILE.exists():
        return ""

    content = QUICK_TUNNEL_LOG_FILE.read_text(encoding="utf-8", errors="replace")
    matches = QUICK_TUNNEL_URL_PATTERN.findall(content)
    return matches[-1] if matches else ""


def fetch_metrics_snapshot(timeout_seconds: float = 2.0) -> dict:
    settings = load_settings()
    url = metrics_url(settings)
    started_at = time.perf_counter()

    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8", errors="replace")
            latency_ms = round((time.perf_counter() - started_at) * 1000, 1)
            metric_lines = [
                line for line in payload.splitlines() if line and not line.lstrip().startswith("#")
            ]
            return {
                "reachable": response.status == 200,
                "status_code": response.status,
                "latency_ms": latency_ms,
                "url": url,
                "metrics_count": len(metric_lines),
                "sample": metric_lines[:5],
                "error": None,
            }
    except (urllib.error.URLError, TimeoutError) as error:
        latency_ms = round((time.perf_counter() - started_at) * 1000, 1)
        return {
            "reachable": False,
            "status_code": None,
            "latency_ms": latency_ms,
            "url": url,
            "metrics_count": 0,
            "sample": [],
            "error": str(error),
        }


def validate_settings(settings: dict | None = None) -> list[str]:
    settings = normalize_settings(settings or load_settings())
    issues = []

    executable_path = resolve_executable(settings)
    if not executable_path:
        issues.append("cloudflared executable was not found. Install cloudflared or set executable_path.")

    if not settings["tunnel_id"] or settings["tunnel_id"] == DEFAULT_TUNNEL_ID:
        issues.append("Set a real Cloudflare Tunnel UUID in tunnel_id.")

    credentials_path = Path(settings["credentials_file"])
    if not settings["credentials_file"] or DEFAULT_TUNNEL_ID in settings["credentials_file"]:
        issues.append("Set credentials_file to the tunnel JSON credentials path.")
    elif not credentials_path.exists():
        issues.append(f"credentials_file does not exist: {credentials_path}")

    if not settings["hostname"] or settings["hostname"] == DEFAULT_HOSTNAME:
        issues.append("Set a real public hostname, for example doc.example.com.")

    if not settings["service_url"].startswith(("http://", "https://")):
        issues.append("service_url must start with http:// or https://")

    return issues


def get_setup_commands(settings: dict | None = None) -> list[str]:
    settings = normalize_settings(settings or load_settings())
    executable = settings["executable_path"] or "cloudflared"
    config_path = str(TUNNEL_CONFIG_FILE)
    return [
        f'{executable} login',
        f'{executable} tunnel create {settings["tunnel_name"]}',
        f'{executable} tunnel route dns {settings["tunnel_id"]} {settings["hostname"]}',
        f'{executable} tunnel --config "{config_path}" --loglevel {settings["log_level"]} --metrics {settings["metrics_address"]} run {settings["tunnel_id"]}',
    ]


def get_tunnel_snapshot() -> dict:
    settings = load_settings()
    pid = read_pid()
    running = bool(pid and process_is_running(pid))

    if pid and not running:
        clear_pid()
        pid = None

    executable = resolve_executable(settings)
    metrics = fetch_metrics_snapshot(timeout_seconds=1.0) if running else {
        "reachable": False,
        "status_code": None,
        "latency_ms": None,
        "url": metrics_url(settings),
        "metrics_count": 0,
        "sample": [],
        "error": "Tunnel is stopped.",
    }

    return {
        "running": running,
        "pid": pid,
        "public_url": public_url(settings),
        "hostname": settings["hostname"],
        "service_url": settings["service_url"],
        "metrics_url": metrics_url(settings),
        "config_path": str(TUNNEL_CONFIG_FILE),
        "settings_path": str(TUNNEL_SETTINGS_FILE),
        "log_path": str(TUNNEL_LOG_FILE),
        "config_exists": TUNNEL_CONFIG_FILE.exists(),
        "settings": settings,
        "validation_errors": validate_settings(settings),
        "executable_path": executable or settings["executable_path"] or "",
        "executable_found": bool(executable),
        "metrics": metrics,
        "setup_commands": get_setup_commands(settings),
    }


def wait_for_tunnel(timeout_seconds: int = 15) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        snapshot = get_tunnel_snapshot()
        if snapshot["running"] and snapshot["metrics"]["reachable"]:
            return True
        time.sleep(0.5)
    return False


def wait_for_quick_tunnel(timeout_seconds: int | None = None) -> bool:
    timeout_seconds = timeout_seconds or read_quick_tunnel_wait_seconds()
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        snapshot = get_quick_tunnel_snapshot()
        if snapshot["running"] and snapshot["public_url"]:
            return True
        time.sleep(0.5)
    return False


def start_tunnel() -> int:
    settings = load_settings()
    issues = validate_settings(settings)
    if issues:
        print("Cloudflare Tunnel settings are incomplete:")
        for issue in issues:
            print(f"- {issue}")
        print(f"Settings file: {TUNNEL_SETTINGS_FILE}")
        print(f"Config file: {write_config(settings)}")
        return 1

    executable = resolve_executable(settings)
    existing_pid = read_pid()
    if existing_pid and process_is_running(existing_pid):
        print(f"Cloudflare Tunnel is already running with PID {existing_pid}")
        print(f"Public URL: {public_url(settings)}")
        return 0

    if existing_pid and not process_is_running(existing_pid):
        clear_pid()

    write_config(settings)
    TUNNEL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    popen_kwargs = {
        "cwd": str(BASE_DIR),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }

    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(
        [
            str(executable),
            "tunnel",
            "--config",
            str(TUNNEL_CONFIG_FILE),
            "--logfile",
            str(TUNNEL_LOG_FILE),
            "--loglevel",
            settings["log_level"],
            "--metrics",
            settings["metrics_address"],
            "--protocol",
            settings["protocol"],
            "run",
            settings["tunnel_id"],
        ],
        **popen_kwargs,
    )
    write_pid(process.pid)

    if not wait_for_tunnel():
        print(f"Cloudflare Tunnel started with PID {process.pid}, but metrics did not respond yet.")
        print(f"Log: {TUNNEL_LOG_FILE}")
        return 1

    print(f"Cloudflare Tunnel started with PID {process.pid}")
    print(f"Public URL: {public_url(settings)}")
    print(f"Config: {TUNNEL_CONFIG_FILE}")
    print(f"Log: {TUNNEL_LOG_FILE}")
    return 0


def stop_tunnel() -> int:
    pid = read_pid()
    if not pid:
        print("Cloudflare Tunnel is not running.")
        return 0

    if not process_is_running(pid):
        clear_pid()
        print("Found stale Cloudflare Tunnel PID file. Cleared it.")
        return 0

    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(result.stdout.strip() or result.stderr.strip() or "Failed to stop Cloudflare Tunnel.")
            return 1
    else:
        os.kill(pid, signal.SIGTERM)

    clear_pid()
    print(f"Cloudflare Tunnel stopped (PID {pid})")
    return 0


def restart_tunnel() -> int:
    stop_tunnel()
    return start_tunnel()


def open_public_url() -> None:
    webbrowser.open(public_url())


def get_quick_tunnel_snapshot() -> dict:
    pid = _read_pid_file(QUICK_TUNNEL_PID_FILE)
    running = bool(pid and process_is_running(pid))

    if pid and not running:
        _clear_pid_file(QUICK_TUNNEL_PID_FILE)
        pid = None

    settings = load_settings()
    executable = resolve_executable(settings)
    public_share_url = read_quick_tunnel_public_url()

    return {
        "running": running,
        "pid": pid,
        "public_url": public_share_url,
        "service_url": settings["service_url"],
        "log_path": str(QUICK_TUNNEL_LOG_FILE),
        "executable_path": executable or settings["executable_path"] or "",
        "executable_found": bool(executable),
    }


def start_quick_tunnel() -> int:
    settings = load_settings()
    executable = resolve_executable(settings)
    if not executable:
        print("cloudflared executable was not found. Install cloudflared or set executable_path.")
        return 1

    existing_pid = _read_pid_file(QUICK_TUNNEL_PID_FILE)
    if existing_pid and process_is_running(existing_pid):
        snapshot = get_quick_tunnel_snapshot()
        print(f"Quick Tunnel is already running with PID {existing_pid}")
        if snapshot["public_url"]:
            print(f"Public URL: {snapshot['public_url']}")
        return 0

    if existing_pid and not process_is_running(existing_pid):
        _clear_pid_file(QUICK_TUNNEL_PID_FILE)

    QUICK_TUNNEL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUICK_TUNNEL_LOG_FILE.write_text("", encoding="utf-8")
    log_handle = QUICK_TUNNEL_LOG_FILE.open("a", encoding="utf-8")

    popen_kwargs = {
        "cwd": str(BASE_DIR),
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }

    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(
        [
            str(executable),
            "tunnel",
            "--url",
            settings["service_url"],
            "--protocol",
            settings["protocol"],
        ],
        **popen_kwargs,
    )
    _write_pid_file(QUICK_TUNNEL_PID_FILE, process.pid)

    quick_tunnel_wait_seconds = read_quick_tunnel_wait_seconds()

    if not wait_for_quick_tunnel(timeout_seconds=quick_tunnel_wait_seconds):
        print(
            "Quick Tunnel started with PID {pid}, but no public URL was detected within {seconds} seconds.".format(
                pid=process.pid,
                seconds=quick_tunnel_wait_seconds,
            )
        )
        print(f"Log: {QUICK_TUNNEL_LOG_FILE}")
        return 1

    snapshot = get_quick_tunnel_snapshot()
    print(f"Quick Tunnel started with PID {process.pid}")
    print(f"Public URL: {snapshot['public_url']}")
    print(f"Log: {QUICK_TUNNEL_LOG_FILE}")
    return 0


def stop_quick_tunnel() -> int:
    pid = _read_pid_file(QUICK_TUNNEL_PID_FILE)
    if not pid:
        print("Quick Tunnel is not running.")
        return 0

    if not process_is_running(pid):
        _clear_pid_file(QUICK_TUNNEL_PID_FILE)
        print("Found stale Quick Tunnel PID file. Cleared it.")
        return 0

    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(result.stdout.strip() or result.stderr.strip() or "Failed to stop Quick Tunnel.")
            return 1
    else:
        os.kill(pid, signal.SIGTERM)

    _clear_pid_file(QUICK_TUNNEL_PID_FILE)
    print(f"Quick Tunnel stopped (PID {pid})")
    return 0


def restart_quick_tunnel() -> int:
    stop_quick_tunnel()
    return start_quick_tunnel()


def open_quick_tunnel_url() -> None:
    url = read_quick_tunnel_public_url()
    if url:
        webbrowser.open(url)


def print_quick_tunnel_status() -> None:
    print(json.dumps(get_quick_tunnel_snapshot(), indent=2, ensure_ascii=False))


def print_tunnel_status() -> None:
    print(json.dumps(get_tunnel_snapshot(), indent=2, ensure_ascii=False))
