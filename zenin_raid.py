"""
Рейд Зенин (Jujutsu Zero) — отдельный от основного рейд-макроса скрипт.

Персонаж стоит на месте. Скрипт раз в цикл смотрит на экран:
    «Ready»  (лобби перед рейдом)        → клик
    «Next»   (Raid Summary: Successful)  → клик
    «Close»  (окно наград)               → клик
    «Retry»  (после закрытия наград)     → клик, рейд начинается заново
    ничего из этого                      → жмёт способности R F X C по кругу
    техника в слоте 1 не выбрана (старт) → жмёт 1
    фаза подмоги дольше --reinf-max с    → респавн (персонажа откинуло в комнату; рейд будет провален)
Бой, волны подкрепления, погоня («Chase took too long…»), чёрные экраны и Domain Expansion
проходятся сами — на них просто продолжаем жать способности.

Запуск:
    python zenin_raid.py                    # F8 — старт/пауза, End — выход
    python zenin_raid.py --on               # начать сразу
    python zenin_raid.py --key-gap 0.2      # пауза между клавишами способностей
    python zenin_raid.py --test snaps/*.png # проверить распознавание кнопок на снимках

Шаблоны кнопок: templates/zenin_*.png (вырезаны из скриншотов окна шириной 1920).
Уведомления (Telegram/Discord) берутся из config.json основного макроса.
"""

import argparse
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import keyboard
import numpy as np
import pyautogui
import pydirectinput
import requests

import vision
from winutil import client_rect, find_window, focus_window, screenshot_png, tg_send, user32

pydirectinput.PAUSE = 0.03
pyautogui.FAILSAFE = False

BASE = Path(__file__).resolve().parent
LOG_PATH = BASE / "zenin.log"
KEY_TOGGLE = "f8"
KEY_EXIT = "end"

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


# Панель задания «Zen'in Reinforcements» слева: пока она висит, идёт фаза подмоги
REINF_ROI = (0.0, 0.30, 0.25, 0.45)


def load_templates():
    tpl = {}
    for name in [n for n, _, _ in BUTTONS] + ["reinforcements"]:
        img = cv2.imread(str(vision.TEMPLATES / f"zenin_{name}.png"), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(vision.TEMPLATES / f"zenin_{name}.png")
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


def reinforcements_visible(img_rgb, tpl, threshold):
    g = cv2.cvtColor(vision.normalize(img_rgb), cv2.COLOR_RGB2GRAY)
    h, w = g.shape
    rx0, ry0, rx1, ry1 = REINF_ROI
    roi = g[int(ry0 * h):int(ry1 * h), int(rx0 * w):int(rx1 * w)]
    return cv2.minMaxLoc(cv2.matchTemplate(roi, tpl["reinforcements"], cv2.TM_CCOEFF_NORMED))[1] >= threshold


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


class ZeninRaid:
    def __init__(self, args):
        self.a = args
        self.tpl = load_templates()
        self.running = threading.Event()
        self.cooldown = {}          # имя кнопки → когда снова можно по ней кликать
        self.key_i = 0
        self.equip_at = 0.0         # когда последний раз жали клавишу техники
        self.reinf_since = None     # когда появилась панель подмоги
        self.reinf_seen = 0.0       # когда её последний раз видели
        self.respawned = False      # в этом рейде уже делали респавн
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
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d} {line}\n")

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

    def status_text(self):
        state = "работает" if self.running.is_set() else "на паузе"
        last = f"{self.last_dur // 60}м {self.last_dur % 60:02d}с" if self.last_dur is not None else "-"
        up = int(time.time() - self.started) // 60
        return (f"Зенин: {state}\nПройдено рейдов за сессию: {self.raids} (сессия {up // 60}ч {up % 60}м)\n"
                f"Последний рейд: {last}\nОкно Roblox: {'есть' if find_window(self.a.window) else 'НЕ НАЙДЕНО'}")

    def tg_commands(self):
        import tgbot

        def toggle():
            self.toggle()
            return "Запущен" if self.running.is_set() else "На паузе"

        return [
            tgbot.Command("status", "ℹ️ Состояние", self.status_text),
            tgbot.Command("toggle", "⏯ Старт / пауза", toggle),
            tgbot.Command("screen", "🖼 Скриншот", lambda: (self.status_text(), screenshot_png())),
        ]

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
            self.respawned = False
            self.reinf_since = None
        self.cooldown[name] = now + COOLDOWN[name]

    def check_reinforcements(self, img):
        """Если фаза подмоги тянется дольше reinf_max секунд — персонажа откинуло в комнату,
        откуда врагов не достать. Тогда респавн (Esc → R → Enter). Игра засчитывает это как
        проваленный рейд, поэтому делаем не больше одного раза за рейд."""
        now = time.time()
        if reinforcements_visible(img, self.tpl, self.a.threshold):
            self.reinf_seen = now
            if self.reinf_since is None:
                self.reinf_since = now
                self.log("фаза подмоги")
        elif self.reinf_since is not None and now - self.reinf_seen > 3:
            # подмог две, перерыв между ними (урон по боссу) меньше 5 с — фазу считаем законченной,
            # когда панели нет 3 с, чтобы у каждой подмоги был свой таймер, а не общий на обе.
            # Ожидание «Chase took too long» после подмоги идёт без панели — там таймер не тикает
            self.log(f"фаза подмоги закончилась за {int(self.reinf_seen - self.reinf_since)} с")
            self.reinf_since = None
        if self.reinf_since is None or self.respawned or now - self.reinf_since < self.a.reinf_max:
            return
        self.respawned = True
        self.reinf_since = None
        self.log(f"фаза подмоги идёт дольше {self.a.reinf_max:.0f} с — застрял в комнате, респавн")
        self.notify(f"⚠️ Зенин: подмога не убивается {self.a.reinf_max:.0f} с — персонаж застрял, делаю респавн "
                    "(рейд засчитается проваленным)", with_screen=True)
        for key in ("esc", "r", "enter"):
            pydirectinput.press(key)
            time.sleep(0.6)
        time.sleep(3)

    def abilities(self):
        """Один круг способностей; между клавишами — key_gap секунд."""
        for _ in range(len(self.a.keys)):
            pydirectinput.press(self.a.keys[self.key_i % len(self.a.keys)])
            self.key_i += 1
            time.sleep(self.a.key_gap)

    def loop(self):
        while True:
            self.running.wait()
            hwnd = find_window(self.a.window)
            if not hwnd or user32.GetForegroundWindow() != hwnd:
                time.sleep(1)  # Roblox свёрнут или закрыт другим окном — ничего не жмём
                continue
            try:
                img = self.grab(hwnd)
                found = find_button(img, self.tpl, self.a.threshold)
            except Exception as e:
                self.log(f"ошибка снимка экрана: {e}")
                time.sleep(1)
                continue
            if found:
                self.on_button(hwnd, *found)
                time.sleep(0.5)
                continue
            if not self.stuck_sent and time.time() - self.last_button > self.a.stuck_min * 60:
                self.stuck_sent = True
                self.log(f"больше {self.a.stuck_min} мин не видно ни одной кнопки рейда — похоже, застрял")
                self.notify(f"⚠️ Зенин: {self.a.stuck_min} мин не видно кнопок рейда, проверь игру", with_screen=True)
            self.check_reinforcements(img)
            # на старте рейда техника не выбрана; повторное нажатие снимает её, поэтому
            # жмём только когда слот виден и тёмный, и не чаще раза в 3 с (экран успевает обновиться)
            if technique_equipped(img) is False and time.time() - self.equip_at > 3:
                self.equip_at = time.time()
                self.log(f"техника не выбрана — жму {self.a.equip_key}")
                pydirectinput.press(self.a.equip_key)
                time.sleep(0.3)
            self.abilities()


def test_mode(files, threshold):
    tpl = load_templates()
    for f in files:
        img = cv2.imread(f)
        if img is None:
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        r = find_button(rgb, tpl, threshold)
        print(f"{Path(f).name}: " + (f"{r[0]} в ({r[1]:.3f}, {r[2]:.3f}), совпадение {r[3]:.2f}" if r else "бой / переход — жмём способности")
              + "; техника " + {True: "выбрана", False: "НЕ выбрана", None: "слотов не видно"}[technique_equipped(rgb)]
              + ("; фаза подмоги" if reinforcements_visible(rgb, tpl, threshold) else ""))


def main():
    ap = argparse.ArgumentParser(description="Рейд Зенин: способности R F X C + кнопки Ready / Next / Close / Retry")
    ap.add_argument("--keys", nargs="+", default=["r", "f", "x", "c"], help="клавиши способностей по кругу")
    ap.add_argument("--equip-key", default="1", help="клавиша выбора техники (слот 1)")
    ap.add_argument("--key-gap", type=float, default=0.25, help="пауза между клавишами способностей, с")
    ap.add_argument("--threshold", type=float, default=0.75, help="порог совпадения шаблона кнопки")
    ap.add_argument("--reinf-max", type=float, default=120,
                    help="через сколько секунд фазы подмоги считать, что застрял в комнате, и делать респавн")
    ap.add_argument("--stuck-min", type=float, default=10, help="через сколько минут без кнопок слать «застрял»")
    ap.add_argument("--window", default="Roblox")
    ap.add_argument("--on", action="store_true", help="начать сразу, без F8")
    ap.add_argument("--test", nargs="*", help="проверить распознавание на снимках и выйти")
    args = ap.parse_args()

    if args.test is not None:
        test_mode(args.test, args.threshold)
        return

    z = ZeninRaid(args)
    threading.Thread(target=z.loop, daemon=True).start()
    keyboard.add_hotkey(KEY_TOGGLE, z.toggle)
    import tgbot
    tgbot.start(z.cfg, "zenin", z.tg_commands(), z.log)
    z.log(f"рейд Зенин: способности {' '.join(k.upper() for k in args.keys)}, "
          f"{KEY_TOGGLE.upper()} — старт/пауза, {KEY_EXIT.title()} — выход")
    if args.on:
        z.toggle()
    keyboard.wait(KEY_EXIT)
    z.running.clear()
    z.log(f"выход, пройдено рейдов: {z.raids}")


if __name__ == "__main__":
    main()
