import hashlib
import json
import os
import queue
import runpy
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None


APP_NAME = "Wed_Send_T0"
REPO_URL = "https://github.com/natthawut-s-gif/Wed_Send_T0.git"
REPO_BRANCH = "main"

ACTIVE_SPLASH = None
STORAGE_ROOT_CACHE: Path | None = None
REGISTRY_PATH = r"Software\Wed_Send_T0\Launcher"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def launcher_path() -> Path:
    return Path(sys.executable if is_frozen() else __file__).resolve()


def data_home() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if root:
            return Path(root)
    xdg_home = os.environ.get("XDG_DATA_HOME")
    if xdg_home:
        return Path(xdg_home)
    return Path.home() / ".local" / "share"


def default_launcher_root() -> Path:
    return data_home() / APP_NAME / "launcher-runtime"


def read_windows_launcher_preferences() -> dict:
    if os.name != "nt" or winreg is None:
        return {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            storage_mode, _ = winreg.QueryValueEx(key, "StorageMode")
            storage_path, _ = winreg.QueryValueEx(key, "StoragePath")
            return {
                "storage_mode": str(storage_mode or "").strip(),
                "storage_path": str(storage_path or "").strip(),
            }
    except OSError:
        return {}


def write_windows_launcher_preferences(storage_mode: str, storage_path: str) -> None:
    if os.name != "nt" or winreg is None:
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
        winreg.SetValueEx(key, "StorageMode", 0, winreg.REG_SZ, storage_mode)
        winreg.SetValueEx(key, "StoragePath", 0, winreg.REG_SZ, storage_path)


def test_writable_directory(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".launcher-write-test"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True, ""
    except OSError as error:
        return False, str(error)


def prompt_for_storage_root(default_path: Path, current_error: str = "") -> Path:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    message = (
        "WedSendT0Launcher needs a writable folder to store:\n"
        "- downloaded runtime code\n"
        "- logs\n"
        "- local launcher state\n\n"
        f"Recommended location:\n{default_path}\n"
    )
    if current_error:
        message += f"\nCurrent issue:\n{current_error}\n"
    message += "\nAllow this app to write inside your Windows user profile?"

    choice = messagebox.askyesnocancel(
        "Storage Permission",
        message,
        parent=root,
    )
    if choice is True:
        root.destroy()
        return default_path
    if choice is None:
        root.destroy()
        raise RuntimeError("Storage permission was cancelled by the user.")

    chosen_dir = filedialog.askdirectory(
        title="Select a writable folder for WedSendT0Launcher data",
        initialdir=str(Path.home()),
        parent=root,
        mustexist=True,
    )
    root.destroy()
    if not chosen_dir:
        raise RuntimeError("No storage folder was selected.")
    return Path(chosen_dir) / APP_NAME / "launcher-runtime"


def resolve_storage_root(*, interactive: bool) -> Path:
    global STORAGE_ROOT_CACHE
    if STORAGE_ROOT_CACHE is not None:
        return STORAGE_ROOT_CACHE

    default_root = default_launcher_root()
    prefs = read_windows_launcher_preferences()
    candidates: list[tuple[str, Path]] = []

    if prefs.get("storage_mode") == "custom" and prefs.get("storage_path"):
        candidates.append(("custom", Path(prefs["storage_path"])))
    if prefs.get("storage_mode") in ("", "localappdata"):
        candidates.append(("localappdata", default_root))
    if not candidates:
        candidates.append(("localappdata", default_root))

    last_error = ""
    for mode, candidate in candidates:
        ok, error = test_writable_directory(candidate)
        if ok:
            STORAGE_ROOT_CACHE = candidate
            if os.name == "nt":
                write_windows_launcher_preferences(mode, str(candidate))
            return candidate
        last_error = error

    if not interactive:
        raise RuntimeError(last_error or "No writable launcher storage directory is available.")

    selected_root = prompt_for_storage_root(default_root, last_error)
    ok, error = test_writable_directory(selected_root)
    if not ok:
        raise RuntimeError(f"Selected storage folder is not writable.\n\n{error}")

    STORAGE_ROOT_CACHE = selected_root
    if os.name == "nt":
        mode = "localappdata" if selected_root == default_root else "custom"
        write_windows_launcher_preferences(mode, str(selected_root))
    return selected_root


def launcher_root() -> Path:
    return resolve_storage_root(interactive=False)


def runtime_repo_dir() -> Path:
    return launcher_root() / "repo"


def launcher_log_file() -> Path:
    return launcher_root() / "launcher.log"


def launcher_state_file() -> Path:
    return launcher_root() / "launcher-state.json"


def launcher_ready_file() -> Path:
    return launcher_root() / "runtime-ready.flag"


def hidden_subprocess_kwargs() -> dict:
    if os.name != "nt":
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def timestamped(message: str) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return f"[{stamp}] {message.rstrip()}"


def log(message: str) -> None:
    line = timestamped(message)
    try:
        launcher_log_file().parent.mkdir(parents=True, exist_ok=True)
        with launcher_log_file().open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
    except OSError:
        pass
    if ACTIVE_SPLASH is not None:
        ACTIVE_SPLASH.post_log(line)


def set_status(message: str) -> None:
    log(message)
    if ACTIVE_SPLASH is not None:
        ACTIVE_SPLASH.post_status(message)


def show_error(title: str, message: str) -> None:
    log(f"ERROR: {title}: {message}")
    if ACTIVE_SPLASH is not None:
        ACTIVE_SPLASH.post_finish(False, f"{title}\n\n{message}")
        return
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        print(f"{title}: {message}")


def get_git_executable() -> str | None:
    candidates = [
        shutil.which("git"),
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\bin\git.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def get_npm_executable() -> str | None:
    candidates = [
        shutil.which("npm"),
        shutil.which("npm.cmd"),
        r"C:\Program Files\nodejs\npm.cmd",
        "/usr/bin/npm",
        "/usr/local/bin/npm",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def get_python_executable() -> str | None:
    candidates = [
        shutil.which("python"),
        r"C:\Program Files\Python313\python.exe",
        r"C:\Program Files\Python312\python.exe",
        r"C:\Program Files\Python311\python.exe",
        "/usr/bin/python3",
        "/usr/local/bin/python3",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def get_pythonw_executable() -> str | None:
    candidates = [
        shutil.which("pythonw"),
        r"C:\Program Files\Python313\pythonw.exe",
        r"C:\Program Files\Python312\pythonw.exe",
        r"C:\Program Files\Python311\pythonw.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    allow_failure: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    log(f"$ {' '.join(command)}")
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Command timed out after {timeout:.0f}s: {' '.join(command)}") from error
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if stdout:
        log(stdout)
    if stderr:
        log(stderr)
    if result.returncode != 0 and not allow_failure:
        raise RuntimeError(stderr or stdout or f"Command failed with exit code {result.returncode}")
    return result


def read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_json_file(path: Path, payload: dict) -> None:
    path.write_text(f"{json.dumps(payload, indent=2)}\n", encoding="utf-8")


def clear_runtime_ready_flag() -> None:
    ready_path = launcher_ready_file()
    try:
        if ready_path.exists():
            ready_path.unlink()
    except OSError:
        pass


def package_lock_hash(repo_dir: Path) -> str:
    lock_path = repo_dir / "package-lock.json"
    package_path = repo_dir / "package.json"
    source_path = lock_path if lock_path.exists() else package_path
    if not source_path.exists():
        return ""
    return hashlib.sha256(source_path.read_bytes()).hexdigest()


def write_env_values(env_path: Path, updates: dict[str, str]) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining_keys = list(updates.keys())
    output_lines: list[str] = []

    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            output_lines.append(line)
            continue

        key, _value = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key in updates:
            output_lines.append(f"{normalized_key}={updates[normalized_key]}")
            if normalized_key in remaining_keys:
                remaining_keys.remove(normalized_key)
        else:
            output_lines.append(line)

    for key in remaining_keys:
        output_lines.append(f"{key}={updates[key]}")

    env_path.write_text(f"{os.linesep.join(output_lines).rstrip()}{os.linesep}", encoding="utf-8")


def ensure_runtime_env(repo_dir: Path) -> None:
    set_status("Preparing local configuration")
    env_example = repo_dir / ".env.example"
    env_path = repo_dir / ".env"
    if not env_path.exists() and env_example.exists():
        env_path.write_text(env_example.read_text(encoding="utf-8"), encoding="utf-8")
        log(f"Created {env_path.name} from .env.example")

    python_command = get_python_executable() if is_frozen() else sys.executable
    if not python_command:
        raise RuntimeError("Python was not found. Install Python 3 first.")
    write_env_values(env_path, {"PYTHON_COMMAND": python_command})


def check_runtime_dependencies() -> None:
    set_status("Checking dependencies")
    python_executable = get_python_executable()
    git_executable = get_git_executable()
    npm_executable = get_npm_executable()

    if not python_executable:
        raise RuntimeError("Python was not found. Install Python 3 first.")
    if not git_executable:
        raise RuntimeError("Git was not found. Install Git first.")
    if not npm_executable:
        raise RuntimeError("npm was not found. Install Node.js first.")

    log(f"Python: {python_executable}")
    log(f"Git: {git_executable}")
    log(f"npm: {npm_executable}")


def update_runtime_repo(repo_dir: Path) -> None:
    git_executable = get_git_executable()
    if not git_executable:
        raise RuntimeError("Git was not found. Install Git first.")

    state = read_json_file(launcher_state_file())
    now = time.time()
    last_git_check = float(state.get("last_git_check_ts", 0) or 0)

    set_status("Checking for updates from GitHub")
    if not (repo_dir / ".git").exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        set_status("Downloading runtime project")
        run_command(
            [
                git_executable,
                "clone",
                "--branch",
                REPO_BRANCH,
                REPO_URL,
                str(repo_dir),
            ],
            timeout=30,
        )
        state["last_git_check_ts"] = now
        write_json_file(launcher_state_file(), state)
        return

    if last_git_check and (now - last_git_check) < 300:
        log(f"Recent Git check found ({now - last_git_check:.0f}s ago). Skipping remote fetch.")
        return

    run_command([git_executable, "remote", "set-url", "origin", REPO_URL], cwd=repo_dir, allow_failure=True)
    local_head = run_command(
        [git_executable, "rev-parse", "HEAD"],
        cwd=repo_dir,
        allow_failure=True,
    ).stdout.strip()
    run_command([git_executable, "fetch", "origin", REPO_BRANCH], cwd=repo_dir, timeout=10)
    remote_head = run_command(
        [git_executable, "rev-parse", "FETCH_HEAD"],
        cwd=repo_dir,
    ).stdout.strip()

    if local_head != remote_head:
        set_status("Updating runtime project")
        run_command([git_executable, "checkout", REPO_BRANCH], cwd=repo_dir)
        run_command([git_executable, "reset", "--hard", "FETCH_HEAD"], cwd=repo_dir)
        log(f"Updated runtime repo: {local_head or '-'} -> {remote_head}")
    else:
        log(f"Runtime repo already up to date at {remote_head}")
    state["last_git_check_ts"] = now
    write_json_file(launcher_state_file(), state)


def ensure_node_dependencies(repo_dir: Path) -> None:
    npm_executable = get_npm_executable()
    if not npm_executable:
        raise RuntimeError("npm was not found. Install Node.js first.")

    state = read_json_file(launcher_state_file())
    current_hash = package_lock_hash(repo_dir)
    node_modules_dir = repo_dir / "node_modules"
    previous_hash = state.get("package_lock_hash", "")

    if node_modules_dir.exists() and current_hash and current_hash == previous_hash:
        log("npm dependencies already up to date.")
        return

    set_status("Installing Node.js dependencies")
    run_command([npm_executable, "install", "--no-fund", "--no-audit"], cwd=repo_dir)
    state["package_lock_hash"] = current_hash
    write_json_file(launcher_state_file(), state)


def run_python_script(script_path: Path, args: list[str]) -> int:
    script_path = script_path.resolve()
    if not script_path.exists():
        show_error("Launcher Error", f"Script not found: {script_path}")
        return 1

    if is_frozen():
        python_executable = get_python_executable()
        if not python_executable:
            show_error("Launcher Error", "Python was not found. Install Python 3 first.")
            return 1

        command = [python_executable, str(script_path), *args]
        result = subprocess.run(
            command,
            cwd=str(script_path.parent),
            capture_output=True,
            text=True,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        return result.returncode

    original_argv = sys.argv[:]
    original_cwd = Path.cwd()
    sys.path.insert(0, str(script_path.parent))
    os.chdir(script_path.parent)
    try:
        sys.argv = [str(script_path), *args]
        runpy.run_path(str(script_path), run_name="__main__")
        return 0
    finally:
        sys.argv = original_argv
        os.chdir(original_cwd)


def run_runtime_ui(repo_dir: Path) -> int:
    ensure_runtime_env(repo_dir)
    ensure_node_dependencies(repo_dir)
    runtime_script = repo_dir / "manage_web_ui.py"

    if is_frozen():
        pythonw_executable = get_pythonw_executable() or get_python_executable()
        if not pythonw_executable:
            show_error("Launcher Error", "Python was not found. Install Python 3 first.")
            return 1

        clear_runtime_ready_flag()
        env = os.environ.copy()
        env["WEDSEND_LAUNCHER_READY_FILE"] = str(launcher_ready_file())
        set_status("Opening application")
        log(f"Launching runtime UI with {pythonw_executable}")
        process = subprocess.Popen(
            [pythonw_executable, str(runtime_script)],
            cwd=str(repo_dir),
            env=env,
            **hidden_subprocess_kwargs(),
        )
        set_status("Waiting for app window")
        ready_path = launcher_ready_file()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if ready_path.exists():
                log("Runtime UI reported ready.")
                return 0

            exit_code = process.poll()
            if exit_code is not None:
                raise RuntimeError(f"Runtime UI exited early with code {exit_code}.")
            time.sleep(0.2)

        if process.poll() is None:
            log("Runtime UI is still starting; continuing without waiting longer.")
            return 0
        raise RuntimeError("Runtime UI did not become ready.")

    return run_python_script(runtime_script, [])


def bootstrap_runtime_and_run() -> int:
    repo_dir = runtime_repo_dir()
    log("--- launcher start ---")
    log(f"Launcher path: {launcher_path()}")
    log(f"Runtime repo: {repo_dir}")

    try:
        check_runtime_dependencies()
        update_runtime_repo(repo_dir)
    except Exception as error:
        if not (repo_dir / "manage_web_ui.py").exists():
            show_error("Launcher Error", f"Unable to download runtime project.\n\n{error}")
            return 1
        log(f"Git update failed, using local runtime copy: {error}")
        set_status("Git update failed, using last downloaded code")

    try:
        return run_runtime_ui(repo_dir)
    except Exception as error:
        show_error("Launcher Error", f"Unable to run runtime project.\n\n{error}")
        return 1


class LauncherSplash:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Starting WedSendT0")
        self.root.geometry("720x460")
        self.root.minsize(680, 420)
        self.root.configure(bg="#f4f1ea")

        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.result_code = 1

        self.status_var = tk.StringVar(value="Preparing launcher")
        self.footer_var = tk.StringVar(value=str(launcher_log_file()))

        shell = ttk.Frame(self.root, padding=16)
        shell.pack(fill="both", expand=True)

        card = ttk.Frame(shell, padding=18)
        card.pack(fill="both", expand=True)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(3, weight=1)

        ttk.Label(card, text="LAUNCHER", foreground="#9a6a2f", font=("Segoe UI", 9, "bold")).grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Label(card, text="Starting WedSendT0", font=("Segoe UI", 20, "bold")).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(4, 2),
        )
        ttk.Label(
            card,
            textvariable=self.status_var,
            foreground="#4b5563",
            font=("Segoe UI", 10),
        ).grid(row=2, column=0, sticky="w", pady=(0, 12))

        self.progress = ttk.Progressbar(card, mode="indeterminate")
        self.progress.grid(row=3, column=0, sticky="ew")
        self.progress.start(10)

        log_card = ttk.Frame(card, padding=12)
        log_card.grid(row=4, column=0, sticky="nsew", pady=(12, 0))
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)
        ttk.Label(log_card, text="Launcher Log", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")

        text_shell = ttk.Frame(log_card)
        text_shell.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        text_shell.columnconfigure(0, weight=1)
        text_shell.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            text_shell,
            height=16,
            wrap="word",
            bg="#fffdf8",
            fg="#1f2937",
            relief="flat",
            borderwidth=0,
            font=("Consolas", 10),
        )
        scroll = ttk.Scrollbar(text_shell, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(state="disabled")

        footer = ttk.Frame(card)
        footer.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.footer_var, foreground="#6b7280", font=("Segoe UI", 9)).grid(
            row=0,
            column=0,
            sticky="w",
        )
        self.close_button = ttk.Button(footer, text="Close", command=self.root.destroy, state="disabled")
        self.close_button.grid(row=0, column=1, sticky="e")

        self.root.after(120, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        self.root.destroy()

    def post_log(self, line: str) -> None:
        self.events.put(("log", line))

    def post_status(self, message: str) -> None:
        self.events.put(("status", message))

    def post_finish(self, success: bool, message: str) -> None:
        self.events.put(("finish_ok" if success else "finish_error", message))

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{line}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _drain_events(self) -> None:
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if event == "log":
                self._append_log(payload)
            elif event == "status":
                self.status_var.set(payload)
            elif event == "finish_ok":
                self.result_code = 0
                self.status_var.set(payload)
                self.progress.stop()
                self.root.after(700, self.root.destroy)
            elif event == "finish_error":
                self.result_code = 1
                self.status_var.set("Launcher failed")
                self.progress.stop()
                self._append_log(payload)
                self.close_button.configure(state="normal")

        if self.root.winfo_exists():
            self.root.after(120, self._drain_events)

    def start(self) -> int:
        worker = threading.Thread(target=self._worker, daemon=True)
        worker.start()
        self.root.mainloop()
        return self.result_code

    def _worker(self) -> None:
        exit_code = bootstrap_runtime_and_run()
        if exit_code == 0:
            self.post_finish(True, "Runtime application started.")
        else:
            self.post_finish(False, "Unable to start the runtime application. Review the launcher log.")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".py"):
        return run_python_script(Path(sys.argv[1]), sys.argv[2:])

    try:
        resolve_storage_root(interactive=True)
    except Exception as error:
        show_error("Launcher Error", str(error))
        return 1

    global ACTIVE_SPLASH
    splash = LauncherSplash()
    ACTIVE_SPLASH = splash
    try:
        return splash.start()
    finally:
        ACTIVE_SPLASH = None


if __name__ == "__main__":
    raise SystemExit(main())
