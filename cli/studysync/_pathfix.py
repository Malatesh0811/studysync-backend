"""
Auto-fix for the `study` command not being on PATH after pip install.

Strategy
--------
1. Write study.bat into the directory that already contains python.exe
   (that dir is on PATH by definition, so the bat works *immediately*
   in the current terminal - no restart needed).
2. Also add the Scripts dir to the user's registry PATH so future
   terminals find the native study.exe directly.

Triggered via studysync_pathfix.pth installed into site-packages.
"""
import os
import sys


def _fix() -> None:
    if sys.platform != "win32":
        return

    # --- 1. Create study.bat shim (works in current terminal immediately) ---
    _create_bat_shim()

    # --- 2. Fix registry PATH (works for new terminals + native study.exe) ---
    sentinel = os.path.join(os.path.expanduser("~"), ".study", ".path_fixed")
    if not os.path.exists(sentinel):
        _fix_registry()
        try:
            os.makedirs(os.path.dirname(sentinel), exist_ok=True)
            open(sentinel, "w").close()
        except Exception:
            pass


def _create_bat_shim() -> None:
    """Write study.bat to the first writable user-space directory on PATH."""
    import shutil

    python_exe = sys.executable
    bat_content = '@echo off\r\n"' + python_exe + '" -m studysync %*\r\n'

    # Candidate directories in priority order:
    #   a) dirname(shutil.which("python")) — the python dir actually on PATH
    #   b) dirname(sys.executable) — where Python really lives
    #   c) every directory currently in PATH
    candidates: list = []

    python_on_path = shutil.which("python") or shutil.which("python3")
    if python_on_path:
        candidates.append(os.path.dirname(python_on_path))

    candidates.append(os.path.dirname(python_exe))

    path_env = os.environ.get("PATH", "")
    candidates.extend(path_env.split(os.pathsep))

    user_home     = os.path.expanduser("~").lower()
    appdata       = os.environ.get("APPDATA", "").lower()
    localappdata  = os.environ.get("LOCALAPPDATA", "").lower()

    seen: set = set()
    for d in candidates:
        if not d:
            continue
        d = os.path.normpath(d)
        if d in seen:
            continue
        seen.add(d)

        if not os.path.isdir(d):
            continue

        # Only touch user-space directories (avoids system dirs that need admin)
        dl = d.lower()
        if not (dl.startswith(user_home) or
                dl.startswith(appdata) or
                dl.startswith(localappdata)):
            continue

        bat_path = os.path.join(d, "study.bat")
        if os.path.exists(bat_path):
            return  # already installed, nothing to do

        try:
            with open(bat_path, "w") as f:
                f.write(bat_content)
            return  # success
        except (PermissionError, OSError):
            continue  # try next candidate


def _fix_registry() -> None:
    """Add the Python Scripts directory to the user's registry PATH."""
    exe_dir = os.path.dirname(sys.executable)
    candidates = [
        os.path.join(exe_dir, "Scripts"),
        os.path.normpath(os.path.join(exe_dir, "..", "Scripts")),
    ]
    try:
        import site as _s
        candidates.append(
            os.path.join(os.path.dirname(_s.getusersitepackages()), "Scripts")
        )
    except Exception:
        pass

    scripts_dir = next(
        (os.path.normpath(c) for c in candidates if os.path.isdir(c)), None
    )
    if not scripts_dir:
        return

    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        )
        try:
            reg_path, _ = winreg.QueryValueEx(key, "PATH")
        except FileNotFoundError:
            reg_path = ""

        if scripts_dir.lower() not in reg_path.lower():
            new_path = reg_path + ";" + scripts_dir if reg_path else scripts_dir
            winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, new_path)
            try:
                import ctypes
                ctypes.windll.user32.SendMessageTimeoutW(
                    0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None
                )
            except Exception:
                pass

        winreg.CloseKey(key)
    except Exception:
        pass


_fix()
