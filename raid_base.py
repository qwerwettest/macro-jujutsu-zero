"""Общая часть рейд-макроса: кнопки рейда, клики, способности, старт/пауза, уведомления.

Кнопки (Ready / Next / Close / Retry) ищутся по шаблонам templates/button_*.png,
вырезанным из скриншотов окна шириной 1920. Уведомления (Telegram/Discord) берутся из config.json.
"""

import json
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pyautogui
import pydirectinput
import requests

import vision
from winutil import client_rect, find_window, focus_window, screenshot_png, tg_send

pydirectinput.PAUSE = 0.03
pyautogui.FAILSAFE = False

BASE = Path(__file__).resolve().parent

# Кнопки в порядке проверки: (имя, область поиска в долях окна, пауза после клика, с).
# Next/Close висят поверх экрана с Retry, поэтому идут раньше него.
# После Ready/Retry пауза длинная: повторный клик может снять голос «готов».
BUTTONS = [
    ("next", (0.35, 0.62, 0.65, 0.84), 2.0),
    ("close", (0.35, 0.62, 0.65, 0.84), 2.0),
    ("retry", (0.30, 0.00, 0.55, 0.16), 10.0),
    ("ready", (0.35, 0.03, 0.65, 0.22), 10.0),
]
COOLDOWN = {name: c for name, _, c in BUTTONS}
LABELS = {"next": "итоги рейда → Next", "close": "награды → Close",
          "retry": "Retry — рейд заново", "ready": "лобби → Ready"}


def load_templates():
    tpl = {}
    for name, _, _ in BUTTONS:
        path = vision.TEMPLATES / f"button_{name}.png"
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(path)
        tpl[name] = img
    return tpl


def find_button(img_rgb, tpl, threshold):
    """Первая найденная кнопка: (имя, x, y в долях окна, совпадение) или None."""
    g = cv2.cvtColor(vision.normalize(img_rgb), cv2.COLOR_RGB2GRAY)
    h, w = g.shape
    for name, (rx0, ry0, rx1, ry1), _ in BUTTONS:
        x0, y0 = int(rx0 * w), int(ry0 * h)
        roi = g[y0:int(ry1 * h), x0:int(rx1 * w)]
        t = tpl[name]
        _, score, _, (mx, my) = cv2.minMaxLoc(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED))
        if score >= threshold:
            return name, (x0 + mx + t.shape[1] / 2) / w, (y0 + my + t.shape[0] / 2) / h, score
    return None


def technique_equipped(img_rgb):
    """Выбрана ли техника в слоте 1: True / False, None — панели слотов не видно (чёрный экран).

    Выбранный слот заметно светлее соседнего слота 2. Сравниваем со слотом 2, а не с числом —
    поверх итогов рейда экран затемнён."""
    g = cv2.cvtColor(vision.normalize(img_rgb), cv2.COLOR_RGB2GRAY)
    h = g.shape[0]
    slot1 = g[h - 70:h - 25, 788:833].mean()
    slot2 = g[h - 70:h - 25, 863:908].mean()
    if slot1 - slot2 > 40:
        return True
    return False if slot2 > 50 else None


class RaidBase:
    """Основа макроса рейда. Наследник задаёт log(), loop(), status_text() и tg_commands()."""

    def __init__(self, args):
        self.a = args
        self.tpl = load_templates()
        self.running = threading.Event()
        self.cooldown = {}          # имя кнопки → когда снова можно по ней кликать
        self.key_i = 0
        self.equip_at = 0.0         # когда последний раз жали клавишу техники
        self.raids = 0
        self.raid_start = None      # когда нажали Ready/Retry
        self.last_button = time.time()
        self.stuck_sent = False
        self.last_dur = None
        self.started = time.time()
        self.notify_cfg = {}
        self.cfg = {}
        cfg_path = BASE / "config.json"
        if cfg_path.exists():
            cfg = self.cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.notify_cfg = {"discord": cfg.get("discord_webhook"), "telegram": cfg.get("telegram") or {}}

    def log(self, msg):
        print(msg, flush=True)

    def notify(self, text, with_screen=False):
        img = screenshot_png() if with_screen else None
        hook = self.notify_cfg.get("discord")
        if hook:
            try:
                if img:
                    requests.post(hook, data={"content": text}, files={"file": ("screen.png", img)}, timeout=15)
                else:
                    requests.post(hook, json={"content": text}, timeout=10)
            except Exception as e:
                self.log(f"discord webhook error: {e}")
        tg = self.notify_cfg.get("telegram", {})
        if tg.get("bot_token") and tg.get("allowed_chat_id"):
            tg_send(tg["bot_token"], tg["allowed_chat_id"], text, img)

    def toggle(self):
        if self.running.is_set():
            self.running.clear()
            self.log("пауза")
        else:
            hwnd = find_window(self.a.window)
            if hwnd:
                focus_window(hwnd)
            self.last_button = time.time()
            self.stuck_sent = False
            self.running.set()
            self.log("старт")

    def grab(self, hwnd):
        x0, y0, w, h = client_rect(hwnd)
        return np.asarray(pyautogui.screenshot(region=(x0, y0, w, h)).convert("RGB"))

    def click(self, hwnd, fx, fy):
        x0, y0, w, h = client_rect(hwnd)
        x, y = int(x0 + fx * w), int(y0 + fy * h)
        # Roblox иногда не видит клик без движения мыши — шевелим курсор
        pydirectinput.moveTo(x, y)
        time.sleep(0.05)
        pydirectinput.moveRel(2, 0)
        pydirectinput.moveRel(-2, 0)
        time.sleep(0.05)
        pydirectinput.click()

    def on_button(self, hwnd, name, fx, fy, score):
        now = time.time()
        self.last_button = now
        self.stuck_sent = False
        if now < self.cooldown.get(name, 0):
            return
        if name == "next" and self.raid_start is not None:
            self.raids += 1
            dur = int(now - self.raid_start)
            self.raid_start = None
            self.last_dur = dur
            self.log(f"рейд #{self.raids} пройден за {dur // 60}м {dur % 60:02d}с")
        self.log(f"{LABELS[name]} ({score:.2f})")
        self.click(hwnd, fx, fy)
        if name in ("ready", "retry"):
            self.raid_start = now
        self.cooldown[name] = now + COOLDOWN[name]

    def abilities(self):
        """Один круг способностей; между клавишами — key_gap секунд."""
        for _ in range(len(self.a.keys)):
            pydirectinput.press(self.a.keys[self.key_i % len(self.a.keys)])
            self.key_i += 1
            time.sleep(self.a.key_gap)
