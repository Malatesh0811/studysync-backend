"""Auto-add Python Scripts dir to Windows PATH on first import (via .pth hook)."""
import os, sys

def _fix():
    if sys.platform != "win32":
        return
    sentinel = os.path.join(os.path.expanduser("~"), ".study", ".path_fixed")
    if os.path.exists(sentinel):
        return
    exe_dir = os.path.dirname(sys.executable)
    candidates = [
        os.path.join(exe_dir, "Scripts"),
        os.path.normpath(os.path.join(exe_dir, "..", "Scripts")),
    ]
    try:
        import site as _s
        candidates.append(os.path.join(os.path.dirname(_s.getusersitepackages()), "Scripts"))
    except Exception:
        pass
    scripts_dir = next((os.path.normpath(c) for c in candidates if os.path.isdir(c)), None)
    if not scripts_dir:
        return
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                              winreg.KEY_READ | winreg.KEY_WRITE)
        try:
            reg_path, _ = winreg.QueryValueEx(key, "PATH")
        except FileNotFoundError:
            reg_path = ""
        if scripts_dir.lower() not in reg_path.lower():
            new_path = reg_path + ";" + scripts_dir if reg_path else scripts_dir
            winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, new_path)
            try:
                import ctypes
                ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)
            except Exception:
                pass
        winreg.CloseKey(key)
        os.makedirs(os.path.dirname(sentinel), exist_ok=True)
        open(sentinel, "w").close()
    except Exception:
        pass

_fix()
