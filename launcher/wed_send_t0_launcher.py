import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox


APP_NAME = "Wed_Send_T0"
REPO_URL = "https://github.com/natthawut-s-gif/Wed_Send_T0.git"
REPO_BRANCH = "main"


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


def launcher_root() -> Path:
    path = data_home() / APP_NAME / "launcher-runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_repo_dir() -> Path:
    return launcher_root() / "repo"


def launcher_log_file() -> Path:
    return launcher_root() / "launcher.log"


def launcher_state_file() -> Path:
    return launcher_root() / "launcher-state.json"


def log(message: str) -> None:
    launcher_log_file().parent.mkdir(parents=True, exist_ok=True)
    with launcher_log_file().open("a", encoding="utf-8") as handle:
        handle.write(f"{message.rstrip()}\n")


def show_error(title: str, message: str) -> None:
    log(f"ERROR: {title}: {message}")
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


def run_command(command: list[str], *, cwd: Path | None = None, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    log(f"$ {' '.join(command)}")
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
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
    env_example = repo_dir / ".env.example"
    env_path = repo_dir / ".env"
    if not env_path.exists() and env_example.exists():
        env_path.write_text(env_example.read_text(encoding="utf-8"), encoding="utf-8")

    python_command = get_python_executable() if is_frozen() else sys.executable
    if not python_command:
        raise RuntimeError("Python was not found. Install Python 3 first.")
    write_env_values(env_path, {"PYTHON_COMMAND": python_command})


def update_runtime_repo(repo_dir: Path) -> None:
    git_executable = get_git_executable()
    if not git_executable:
        raise RuntimeError("Git was not found. Install Git first.")

    if not (repo_dir / ".git").exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            [
                git_executable,
                "clone",
                "--branch",
                REPO_BRANCH,
                REPO_URL,
                str(repo_dir),
            ]
        )
        return

    run_command([git_executable, "remote", "set-url", "origin", REPO_URL], cwd=repo_dir, allow_failure=True)
    local_head = run_command(
        [git_executable, "rev-parse", "HEAD"],
        cwd=repo_dir,
        allow_failure=True,
    ).stdout.strip()
    run_command([git_executable, "fetch", "origin", REPO_BRANCH], cwd=repo_dir)
    remote_head = run_command(
        [git_executable, "rev-parse", "FETCH_HEAD"],
        cwd=repo_dir,
    ).stdout.strip()

    if local_head != remote_head:
        run_command([git_executable, "checkout", REPO_BRANCH], cwd=repo_dir)
        run_command([git_executable, "reset", "--hard", "FETCH_HEAD"], cwd=repo_dir)
        log(f"Updated runtime repo: {local_head or '-'} -> {remote_head}")
    else:
        log(f"Runtime repo already up to date at {remote_head}")


def ensure_node_dependencies(repo_dir: Path) -> None:
    npm_executable = get_npm_executable()
    if not npm_executable:
        log("npm was not found. Skipping npm install.")
        return

    state = read_json_file(launcher_state_file())
    current_hash = package_lock_hash(repo_dir)
    node_modules_dir = repo_dir / "node_modules"
    previous_hash = state.get("package_lock_hash", "")

    if node_modules_dir.exists() and current_hash and current_hash == previous_hash:
        log("npm dependencies already up to date.")
        return

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
        result = subprocess.run(command, cwd=str(script_path.parent), capture_output=True, text=True, check=False)
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

        log(f"Launching runtime UI with {pythonw_executable}")
        subprocess.Popen([pythonw_executable, str(runtime_script)], cwd=str(repo_dir))
        return 0

    return run_python_script(runtime_script, [])


def bootstrap_runtime_and_run() -> int:
    repo_dir = runtime_repo_dir()
    log("--- launcher start ---")
    log(f"Launcher path: {launcher_path()}")
    log(f"Runtime repo: {repo_dir}")

    try:
        update_runtime_repo(repo_dir)
    except Exception as error:
        if not (repo_dir / "manage_web_ui.py").exists():
            show_error("Launcher Error", f"Unable to download runtime project.\n\n{error}")
            return 1
        log(f"Git update failed, using local runtime copy: {error}")

    try:
        return run_runtime_ui(repo_dir)
    except Exception as error:
        show_error("Launcher Error", f"Unable to run runtime project.\n\n{error}")
        return 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".py"):
        return run_python_script(Path(sys.argv[1]), sys.argv[2:])

    return bootstrap_runtime_and_run()


if __name__ == "__main__":
    raise SystemExit(main())
