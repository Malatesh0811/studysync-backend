"""
_pathfix.py — Auto-adds the Python Scripts directory to the user's
Windows PATH (registry) the first time this package is imported.

Triggered via studysync_pathfix.pth installed alongside the package.
Runs silently; uses a sentinel file so it only acts once.
"""
import os
import sys


def _ensure_scripts_on_path() -> None:
    # Only needed on Windows
    if sys.platform != "win32":
        return

    # Sentinel — skip if already done
    sentinel = os.path.join(os.path.expanduser("~"), ".study", ".path_fixed")
    if os.path.exists(sentinel):
        return

    # Find the Scripts directory that contains study.exe
    exe_dir = os.path.dirname(sys.executable)
    candidates = [
        os.path.join(exe_dir, "Scripts"),
        os.path.normpath(os.path.join(exe_dir, "..", "Scripts")),
    ]
    try:
        import site as _site
        user_data = os.path.dirname(_site.getusersitepackages())
        candidates.append(os.path.join(user_data, "Scripts"))
    except Exception:
        pass

    scripts_dir = next(
        (os.path.normpath(c) for c in candidates if os.path.isdir(c)), None
    )
    if not scripts_dir:
        return

    try:
        import winreg  # type: ignore[import]

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        )
        try:
            reg_path, _ = winreg.QueryValueEx(key, "PATH")
        except FileNotFoundError:
            reg_path = ""

        # Add only if missing
        if scripts_dir.lower() not in reg_path.lower():
            new_path = f"{reg_path};{scripts_dir}" if reg_path else scripts_dir
            winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, new_path)

            # Broadcast the change so new terminals pick it up immediately
            try:
                import ctypes
                ctypes.windll.user32.SendMessageTimeoutW(
                    0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None
                )
            except Exception:
                pass

        winreg.CloseKey(key)

        # Write sentinel so this never runs again
        os.makedirs(os.path.dirname(sentinel), exist_ok=True)
        with open(sentinel, "w") as f:
            f.write(scripts_dir)

    except Exception:
        pass  # Never crash the user's Python


_ensure_scripts_on_path()
