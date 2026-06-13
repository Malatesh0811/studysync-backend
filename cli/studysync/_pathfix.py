"""
Auto-fix for `study` not being on PATH after pip install.

What we do
----------
1. Write study.bat to %LOCALAPPDATA%\Microsoft\WindowsApps\ — this directory
   is on the default PATH of every Windows 10/11 machine, so the bat works
   in every NEW terminal with zero user action.
2. Also write to the directory containing python.exe and any other writable
   user-space PATH directory as fallbacks.
3. Add the Python Scripts dir to the registry PATH for native study.exe.

Limitation
----------
cmd.exe reads PATH once at startup. No Python code can update an already-running
cmd.exe. So in the SAME terminal where pip was run, use:
    python -m studysync <command>
In ANY new terminal opened after install, `study` works normally.

Triggered via studysync_pathfix.pth installed into site-packages.
"""
import os
import sys


def _fix() -> None:
    if sys.platform != "win32":
        return
    _create_bat_shim()
    sentinel = os.path.join(os.path.expanduser("~"), ".study", ".path_fixed")
    if not os.path.exists(sentinel):
        _fix_registry()
        try:
            os.makedirs(os.path.dirname(sentinel), exist_ok=True)
            open(sentinel, "w").close()
        except Exception:
            pass


def _create_bat_shim() -> None:
    import shutil

    python_exe = sys.executable
    bat_content = '@echo off\r\n"' + python_exe + '" -m studysync %*\r\n'

    localappdata = os.environ.get("LOCALAPPDATA", "")
    appdata = os.environ.get("APPDATA", "")
    user_home = os.path.expanduser("~")

    # Priority-ordered candidate directories
    candidates = []

    # 1. WindowsApps — on PATH by default on every Windows 10/11 machine
    if localappdata:
        candidates.append(os.path.join(localappdata, "Microsoft", "WindowsApps"))

    # 2. dirname of python.exe that's on PATH (shutil.which finds it)
    python_on_path = shutil.which("python") or shutil.which("python3")
    if python_on_path:
        candidates.append(os.path.dirname(python_on_path))

    # 3. dirname of sys.executable (the real Python binary)
    candidates.append(os.path.dirname(python_exe))

    # 4. Every directory currently in PATH (user-space only)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        candidates.append(d)

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

        # Only touch user-space directories
        dl = d.lower()
        uh = user_home.lower()
        ad = appdata.lower()
        la = localappdata.lower()
        if not (dl.startswith(uh) or dl.startswith(ad) or dl.startswith(la)):
            continue

        bat_path = os.path.join(d, "study.bat")
        if os.path.exists(bat_path):
            return  # already installed

        try:
            with open(bat_path, "w") as f:
                f.write(bat_content)
            return  # success
        except (PermissionError, OSError):
            continue


def _fix_registry() -> None:
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
