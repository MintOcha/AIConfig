"""Start/stop Studio Play with native shortcuts in the visible matching window."""
import ctypes
import time
from ctypes import wintypes as w

_GUI_MENU_FLAGS = 0x04 | 0x08 | 0x10


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [('cbSize', w.DWORD), ('flags', w.DWORD),
                ('hwndActive', w.HWND), ('hwndFocus', w.HWND),
                ('hwndCapture', w.HWND), ('hwndMenuOwner', w.HWND),
                ('hwndMoveSize', w.HWND), ('hwndCaret', w.HWND), ('rcCaret', w.RECT)]


def _open_menus(user, hwnd):
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.GetWindowThreadProcessId.restype = w.DWORD
    user.GetGUIThreadInfo.argtypes = [w.DWORD, ctypes.POINTER(_GuiThreadInfo)]
    user.GetGUIThreadInfo.restype = w.BOOL
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [w.HWND]
    process_id = w.DWORD()
    thread_id = user.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not thread_id:
        raise RuntimeError('Cannot inspect Studio GUI thread; no shortcut was sent')
    state = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
    if not user.GetGUIThreadInfo(thread_id, ctypes.byref(state)):
        raise RuntimeError('Cannot inspect Studio menu state; no shortcut was sent')
    menus = [state.hwndMenuOwner or hwnd] if state.flags & _GUI_MENU_FLAGS else []
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def collect(handle, _):
        if not user.IsWindowVisible(handle):
            return True
        owner = w.DWORD()
        owner_thread = user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner_thread != thread_id or owner.value != process_id.value:
            return True
        name = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(handle, name, len(name))
        # Qt popups do not necessarily enter Win32 menu mode.
        if name.value.startswith('Qt') and 'QWindowPopup' in name.value:
            menus.append(handle)
        return True
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.EnumWindows.restype = w.BOOL
    if not user.EnumWindows(callback_type(collect), 0):
        raise RuntimeError('Cannot inspect Studio popups; no shortcut was sent')
    return menus


def _dismiss_menus(user, hwnd):
    if not _open_menus(user, hwnd):
        return
    _shortcut_modifiers(user, False)
    if user.GetForegroundWindow() != hwnd:
        raise RuntimeError('Studio lost foreground before menu dismissal; no shortcut was sent')
    user.keybd_event(0x1B, 0, 0, 0)
    user.keybd_event(0x1B, 0, 2, 0)
    deadline = time.monotonic() + .5
    while True:
        if user.GetForegroundWindow() != hwnd:
            raise RuntimeError('Studio lost foreground during menu dismissal; no shortcut was sent')
        menus = _open_menus(user, hwnd)
        if not menus:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Studio menu remained open after Escape: {menus}; no shortcut was sent')
        time.sleep(.02)


def _shortcut_modifiers(user, stop):
    user.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user.GetAsyncKeyState.restype = w.SHORT
    keys = {'Shift': 0x10, 'LeftShift': 0xA0, 'RightShift': 0xA1,
            'Control': 0x11, 'LeftControl': 0xA2, 'RightControl': 0xA3,
            'Alt': 0x12, 'LeftAlt': 0xA4, 'RightAlt': 0xA5, 'LeftWin': 0x5B, 'RightWin': 0x5C}
    states = {name: user.GetAsyncKeyState(key) & 0xFFFF for name, key in keys.items()}
    held = [name for name, state in states.items() if state & 0x8000]
    conflicts = [name for name in held if not stop or 'Shift' not in name]
    if conflicts:
        shortcut = 'Shift+F5' if stop else 'F5'
        raise RuntimeError(f'Cannot send native {shortcut}: conflicting held modifiers {conflicts}; '
                           f'GetAsyncKeyState values: {states}; no shortcut was sent and no held keys were released')
    return any('Shift' in name for name in held)



def _shortcut(record, stop):
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.GetForegroundWindow.restype=w.HWND
    user.SetForegroundWindow.argtypes=[w.HWND]
    _shortcut_modifiers(user, stop)
    from .windows_capture import focus
    hwnd=focus(record)
    time.sleep(.1)
    if user.GetForegroundWindow()!=hwnd:raise RuntimeError('Windows refused foreground Studio activation')
    _dismiss_menus(user, hwnd)
    shift_held = _shortcut_modifiers(user, stop)
    if user.GetForegroundWindow() != hwnd:
        raise RuntimeError('Studio lost foreground before Play shortcut; no shortcut was sent')
    inject_shift = stop and not shift_held
    if inject_shift:
        user.keybd_event(0x10,0,0,0)
    try:
        user.keybd_event(0x74,0,0,0)
        user.keybd_event(0x74,0,2,0)
    finally:
        if inject_shift:
            user.keybd_event(0x10,0,2,0)


def start(record):
    _shortcut(record, False)


def stop(record):
    _shortcut(record, True)
