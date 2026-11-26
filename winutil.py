"""Окно Roblox (поиск, фокус, координаты), скриншот и отправка в Telegram."""

import ctypes
import ctypes.wintypes as wt
import io

import pyautogui
import requests

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()


def find_window(title):
    hwnd = user32.FindWindowW(None, title)
    return hwnd or None


def client_rect(hwnd):
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, r.right - r.left, r.bottom - r.top


def focus_window(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # нажатие Alt снимает ограничение Windows на SetForegroundWindow
    user32.keybd_event(0x12, 0, 0, 0)
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 2, 0)


def screenshot_png():
    try:
        buf = io.BytesIO()
        img = pyautogui.screenshot()
        img.thumbnail((1280, 1280))
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def tg_send(token, chat_id, text, img=None):
    try:
        base = f"https://api.telegram.org/bot{token}"
        if img:
            requests.post(f"{base}/sendPhoto", data={"chat_id": chat_id, "caption": text},
                          files={"photo": ("screen.png", img)}, timeout=20)
        else:
            requests.post(f"{base}/sendMessage", data={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception as e:
        print(f"telegram error: {e}")
