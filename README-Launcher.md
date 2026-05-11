# Windows EXE Launcher

This launcher project builds a Windows `.exe` that:

1. Checks the Git version from `https://github.com/natthawut-s-gif/Wed_Send_T0.git`
2. Updates the local runtime copy from Git on every launch
3. Runs the latest `manage_web_ui.py` from that Git runtime copy
4. Reuses the launcher itself as `PYTHON_COMMAND` for `ocr_preprocess.py`

## What It Needs

- Git
- Node.js + npm
- PyInstaller for building the launcher

Install PyInstaller on the build machine:

```bash
pip install pyinstaller
```

## Build The EXE

From the project root on Windows:

```bat
launcher\build_launcher.bat
```

Output:

```text
dist\WedSendT0Launcher.exe
```

## How It Works

When `WedSendT0Launcher.exe` opens:

1. It creates a runtime folder under:
   - Windows: `%LOCALAPPDATA%\Wed_Send_T0\launcher-runtime\repo`
2. It clones the Git repo if missing
3. It fetches `origin/main`
4. If a newer commit exists, it resets the runtime repo to that commit
5. It updates `.env` in the runtime repo so `PYTHON_COMMAND` points to the launcher exe
6. It runs `npm install` when `package-lock.json` changes
7. It starts the latest `manage_web_ui.py` from the updated runtime repo

## Important Notes

- The launcher updates tracked code from Git every time it starts
- Runtime settings such as `.env`, webhook settings, and history remain in the runtime repo
- The runtime repo is separate from the original build folder
- If Git update fails but a previous runtime copy already exists, the launcher will try to run that local copy

## Rebuild After Launcher Changes

If you change anything inside:

```text
launcher\
```

build the exe again:

```bat
launcher\build_launcher.bat
```
