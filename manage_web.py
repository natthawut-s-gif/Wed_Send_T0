import argparse
import csv
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from cloudflared_manager import get_setup_commands
from cloudflared_manager import print_quick_tunnel_status
from cloudflared_manager import print_tunnel_status
from cloudflared_manager import restart_quick_tunnel
from cloudflared_manager import restart_tunnel
from cloudflared_manager import start_quick_tunnel
from cloudflared_manager import start_tunnel
from cloudflared_manager import stop_quick_tunnel
from cloudflared_manager import stop_tunnel
from cloudflared_manager import write_config as write_tunnel_config


BASE_DIR = Path(__file__).resolve().parent
PID_FILE = BASE_DIR / ".upload_bridge.pid"
LOG_FILE = BASE_DIR / ".upload_bridge.log"
ENV_FILE = BASE_DIR / ".env"
DEFAULT_PORT = 3000
DEFAULT_HOST = "0.0.0.0"


def read_env_port() -> int:
    if not ENV_FILE.exists():
        return DEFAULT_PORT

    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "PORT":
            try:
                return int(value.strip())
            except ValueError:
                return DEFAULT_PORT

    return DEFAULT_PORT


def read_env_host() -> str:
    if not ENV_FILE.exists():
        return DEFAULT_HOST

    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "HOST":
            host = value.strip()
            return host or DEFAULT_HOST

    return DEFAULT_HOST


def app_url(hostname: str = "localhost") -> str:
    return f"http://{hostname}:{read_env_port()}"


def local_share_urls() -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    try:
        hostname = socket.gethostname()
        for family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
            hostname,
            None,
            socket.AF_INET,
        ):
            if family != socket.AF_INET:
                continue
            address = sockaddr[0]
            if address.startswith("127."):
                continue
            url = app_url(address)
            if url not in seen:
                seen.add(url)
                urls.append(url)
    except socket.gaierror:
        pass

    return urls


def health_url() -> str:
    return f"{app_url()}/health"


def fetch_health_snapshot(timeout_seconds: float = 2.0) -> dict:
    started_at = time.perf_counter()
    try:
        with urllib.request.urlopen(health_url(), timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8")
            latency_ms = round((time.perf_counter() - started_at) * 1000, 1)
            return {
                "reachable": response.status == 200,
                "status_code": response.status,
                "body": json.loads(payload) if payload else None,
                "latency_ms": latency_ms,
                "error": None,
            }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        latency_ms = round((time.perf_counter() - started_at) * 1000, 1)
        return {
            "reachable": False,
            "status_code": None,
            "body": None,
            "latency_ms": latency_ms,
            "error": str(error),
        }


def read_pid() -> int | None:
    if not PID_FILE.exists():
        return None

    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def write_pid(pid: int) -> None:
    PID_FILE.write_text(str(pid), encoding="utf-8")


def clear_pid() -> None:
    if PID_FILE.exists():
        PID_FILE.unlink()


def process_is_running(pid: int) -> bool:
    if pid <= 0:
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


def wait_for_health(timeout_seconds: int = 15) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if fetch_health_snapshot(timeout_seconds=2).get("reachable"):
            return True
        time.sleep(0.5)
    return False


def get_process_memory_mb(pid: int | None) -> float | None:
    if not pid or not process_is_running(pid):
        return None

    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0 or not result.stdout.strip():
            return None

        try:
            row = next(csv.reader([result.stdout.strip().splitlines()[0]]))
            memory_text = row[-1]
            digits = "".join(char for char in memory_text if char.isdigit())
            if not digits:
                return None
            memory_kb = int(digits)
            return round(memory_kb / 1024, 2)
        except (StopIteration, ValueError, IndexError, csv.Error):
            return None

    return None


def get_log_size_kb() -> float:
    if not LOG_FILE.exists():
        return 0.0
    return round(LOG_FILE.stat().st_size / 1024, 2)


def get_status_snapshot() -> dict:
    pid = read_pid()
    running = bool(pid and process_is_running(pid))

    if pid and not running:
        clear_pid()
        pid = None

    health = fetch_health_snapshot(timeout_seconds=1.0) if running else {
        "reachable": False,
        "status_code": None,
        "body": None,
        "error": "Server is stopped.",
    }

    health_body = health.get("body") if isinstance(health.get("body"), dict) else {}
    share_urls = health_body.get("urls") if isinstance(health_body.get("urls"), list) else None
    if not share_urls:
        share_urls = [app_url(), *local_share_urls()]

    preferred_url = next((url for url in share_urls if "localhost" not in url), share_urls[0])

    return {
        "running": running,
        "pid": pid,
        "url": app_url(),
        "share_urls": share_urls,
        "preferred_url": preferred_url,
        "host": read_env_host(),
        "health_url": health_url(),
        "log": str(LOG_FILE),
        "health": health,
        "process": {
            "memory_mb": get_process_memory_mb(pid),
        },
        "log_size_kb": get_log_size_kb(),
    }


def read_log_tail(max_lines: int = 120) -> str:
    if not LOG_FILE.exists():
        return ""

    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def open_site() -> None:
    webbrowser.open(app_url())


def print_status_snapshot() -> None:
    snapshot = get_status_snapshot()

    if not snapshot["running"]:
        print("Server is stopped.")
        return

    print(json.dumps(snapshot, indent=2))


def start_server(open_browser: bool) -> int:
    existing_pid = read_pid()
    if existing_pid and process_is_running(existing_pid):
        print(f"Server is already running with PID {existing_pid} at {app_url()}")
        if open_browser:
            open_site()
        return 0

    if existing_pid and not process_is_running(existing_pid):
        clear_pid()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_handle = LOG_FILE.open("a", encoding="utf-8")

    creationflags = 0
    popen_kwargs = {
        "cwd": str(BASE_DIR),
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }

    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(["node", "server.js"], **popen_kwargs)
    write_pid(process.pid)

    if not wait_for_health():
        print(f"Server process started with PID {process.pid}, but /health did not respond.")
        print(f"Check log: {LOG_FILE}")
        return 1

    print(f"Server started with PID {process.pid}")
    print(f"Local URL: {app_url()}")
    share_urls = get_status_snapshot().get("share_urls", [])
    for share_url in share_urls:
        print(f"Share URL: {share_url}")
    print(f"Log: {LOG_FILE}")

    if open_browser:
        open_site()

    return 0


def stop_server() -> int:
    pid = read_pid()
    if not pid:
        print("Server is not running.")
        return 0

    if not process_is_running(pid):
        clear_pid()
        print("Found stale PID file. Cleared it.")
        return 0

    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(result.stdout.strip() or result.stderr.strip() or "Failed to stop server.")
            return 1
    else:
        os.kill(pid, signal.SIGTERM)

    clear_pid()
    print(f"Server stopped (PID {pid})")
    return 0


def server_status() -> int:
    print_status_snapshot()
    return 0


def restart_server(open_browser: bool) -> int:
    stop_server()
    return start_server(open_browser=open_browser)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start or stop the local upload website."
    )
    parser.add_argument(
        "command",
        choices=[
            "start",
            "stop",
            "restart",
            "status",
            "tunnel-start",
            "tunnel-stop",
            "tunnel-restart",
            "tunnel-status",
            "tunnel-config",
            "quick-share-start",
            "quick-share-stop",
            "quick-share-restart",
            "quick-share-status",
        ],
        help="Action to run",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the website in a browser when starting",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command == "start":
        return start_server(open_browser=not args.no_browser)
    if args.command == "stop":
        return stop_server()
    if args.command == "restart":
        return restart_server(open_browser=not args.no_browser)
    if args.command == "tunnel-start":
        return start_tunnel()
    if args.command == "tunnel-stop":
        return stop_tunnel()
    if args.command == "tunnel-restart":
        return restart_tunnel()
    if args.command == "tunnel-status":
        print_tunnel_status()
        return 0
    if args.command == "tunnel-config":
        config_path = write_tunnel_config()
        print(f"Config file: {config_path}")
        print("Suggested commands:")
        for command in get_setup_commands():
            print(command)
        return 0
    if args.command == "quick-share-start":
        return start_quick_tunnel()
    if args.command == "quick-share-stop":
        return stop_quick_tunnel()
    if args.command == "quick-share-restart":
        return restart_quick_tunnel()
    if args.command == "quick-share-status":
        print_quick_tunnel_status()
        return 0
    return server_status()


if __name__ == "__main__":
    sys.exit(main())
