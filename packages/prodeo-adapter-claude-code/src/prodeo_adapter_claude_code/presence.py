"""Local-input presence: how long since the user last touched this machine.

Platform variance stays behind one seam: :func:`seconds_since_input` answers
in seconds on Windows (``GetLastInputInfo``) and ``None`` ("unknown")
everywhere else. Callers treat unknown as *away* — mediate rather than assume
someone is watching the terminal. Note the Windows counter is machine-wide:
typing in any application counts as being at the station.
"""

import sys
from collections.abc import Callable

#: The injectable seam: () -> seconds since last local input, or None.
SinceInputFn = Callable[[], float | None]


def seconds_since_input() -> float | None:
    """Seconds since the last local keyboard/mouse input, or None if unknowable."""
    if sys.platform != "win32":
        return None
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    try:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = int(ctypes.windll.kernel32.GetTickCount())
    except OSError:
        return None
    # GetTickCount wraps at ~49.7 days; unsigned 32-bit subtraction stays correct.
    return ((now - int(info.dwTime)) & 0xFFFFFFFF) / 1000.0
