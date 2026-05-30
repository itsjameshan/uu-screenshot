import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

titles = []

def callback(hwnd, lparam):
    if user32.IsWindowVisible(hwnd):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if any(k in title for k in ['UU', 'uu', '网易', '远程', '伙伴', '设备', '验证']):
                iconic = user32.IsIconic(hwnd)
                titles.append((title, iconic))
    return True

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows(WNDENUMPROC(callback), 0)

if titles:
    for t, m in titles:
        print(f"[{'MINIMIZED' if m else 'NORMAL'}] {t}")
else:
    print("NO UU windows found")