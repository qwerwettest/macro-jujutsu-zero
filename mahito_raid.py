"""
Рейд Махито (Jujutsu Zero, метро) — отдельный скрипт по образцу рейда Зенин.

Скрипт раз в цикл смотрит на экран и действует по тому, что видит:
    «Ready» / «Retry»                  → клик (рейд начинается)
    техника в слоте 1 не выбрана       → жмёт 1
    маркеры целей (кружок с меткой)    → идёт к маркеру (сначала левый, потом правый), на месте жмёт F;
                                         маркер гаснет — группа убита, идёт к следующему.
                                         Не погас за --kill-wait с — немного сдвигается (НПС застряли
                                         за колонной) и снова жмёт F.
    полоса HP босса «Mahito» сверху    → стоит и жмёт способности R F X C по кругу
                                         (фаза у точки спавна и фаза на путях); при низком HP — уворот Q
    «Next» (Raid Summary)              → клик
    «Close» (награды)                  → считывает кристаллы и сундуки в статистику, клик
Остальное время (переходы, отсчёт) жмёт способности, как в рейде Зенин.

Движение — WASD относительно камеры, по пути серии прыжков (3 нажатия пробела
через 0.1 с, пауза 1 с — пол неровный). Камеру скрипт не крутит.

Статистика (рейды, кристаллы, сундуки) копится в mahito_stats.json.

Запуск:
    python mahito_raid.py                    # F8 — старт/пауза, End — выход
    python mahito_raid.py --on               # начать сразу
    python mahito_raid.py --stats            # показать статистику и выйти
    python mahito_raid.py --test snaps/mahito/*.png   # проверить распознавание на снимках
"""

import argparse
import json
import random
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import keyboard
import numpy as np
import pydirectinput

import digits
import tgbot
import vision
from winutil import find_window, user32
from zenin_raid import ZeninRaid, find_button, load_templates, technique_equipped

pydirectinput.PAUSE = 0.03

BASE = Path(__file__).resolve().parent
LOG_PATH = BASE / "mahito.log"
STATS_PATH = BASE / "mahito_stats.json"
UNREAD_DIR = BASE / "snaps" / "mahito_unread"
KEY_TOGGLE = "f8"
KEY_EXIT = "end"
MOVE_KEYS = ("w", "a", "s", "d")

# Все координаты — в кадре, приведённом к ширине 1920 (vision.normalize), высота 1080.
MARKER_THRESHOLD = 0.8      # маркеры совпадают на 0.95+, всё остальное на экране — не выше 0.55
BOTTOM_STRIP = 160        # нижняя полоса экрана, где маркер ищется по верхней части значка
BOTTOM_THRESHOLD = 0.7
BOSS_ROW = 0.1407           # строка полосы HP босса (152 px из 1080), выше цифр «80,000,000/…»
BOSS_X = (520, 1400)
PLAYER_HP_FROM_BOTTOM = 175  # строка своей полосы HP: 905 px при высоте 1080 (привязана к низу экрана)
PLAYER_HP_X = (670, 1250)
REWARD_BAND = (0.44, 0.58)   # полоса с карточками наград по высоте


def load_mahito_templates():
    tpl = {}
    for name in ("marker", "crystal", "chest"):
        img = cv2.imread(str(vision.TEMPLATES / f"mahito_{name}.png"), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(vision.TEMPLATES / f"mahito_{name}.png")
        tpl[name] = img
    return tpl


def find_markers(norm_rgb, tpl):
    """Маркеры целей: список (x, y, расстояние в м или None); x, y — доли экрана."""
    g = cv2.cvtColor(norm_rgb, cv2.COLOR_RGB2GRAY)
    h, w = g.shape
    t = tpl["marker"]
    th, tw = t.shape
    res = cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED)
    out = []
    for _ in range(4):
        _, score, _, (mx, my) = cv2.minMaxLoc(res)
        if score < MARKER_THRESHOLD:
            break
        cx, cy = mx + tw // 2, my + th // 2
        out.append((cx / w, cy / h, marker_distance(norm_rgb, cx, cy)))
        cv2.rectangle(res, (mx - tw, my - th), (mx + tw, my + th), -1, -1)
    # маркер, прижатый к нижнему краю (цель сзади), бывает наполовину закрыт надписями интерфейса
    # («Server Time» слева внизу): там ищем по верхней части значка — на ней 0.8, на остальном ≤0.45
    top = t[:int(th * 0.45)]
    y0 = h - BOTTOM_STRIP
    res = cv2.matchTemplate(g[y0:], top, cv2.TM_CCOEFF_NORMED)
    for _ in range(2):
        _, score, _, (mx, my) = cv2.minMaxLoc(res)
        if score < BOTTOM_THRESHOLD:
            break
        cx, cy = mx + tw // 2, y0 + my + th // 2
        if all(abs(cx / w - x) > 0.03 or abs(cy / h - y) > 0.05 for x, y, _ in out):
            out.append((cx / w, cy / h, marker_distance(norm_rgb, cx, cy)))
        cv2.rectangle(res, (mx - tw, my - th), (mx + tw, my + th), -1, -1)
    return out


def marker_distance(norm_rgb, cx, cy):
    """Число метров из подписи под маркером («83m» → 83) или None."""
    h, w = norm_rgb.shape[:2]
    crop = norm_rgb[min(h - 1, cy + 20):min(h, cy + 44), max(0, cx - 40):min(w, cx + 40)]
    if crop.size == 0:
        return None
    text = digits.read_text(crop)
    # строго «цифры + m»: если хоть один символ не распознан (подпись перекрыта эффектами),
    # расстояние неизвестно — иначе «26m» с нераспознанной 6 читалось как 2 м
    if len(text) < 2 or len(text) > 4 or text[-1] != "m" or not text[:-1].isdigit():
        return None
    return int(text[:-1])


def boss_hp(norm_rgb):
    """Доля HP босса (0..1) по зелёной полосе сверху или None, если полосы нет."""
    h = norm_rgb.shape[0]
    row = norm_rgb[int(BOSS_ROW * h), BOSS_X[0]:BOSS_X[1]].astype(int)
    r, g, b = row[:, 0], row[:, 1], row[:, 2]
    green = (g > r + 30) & (g > b + 20)
    if green.mean() < 0.85:
        return None
    return float(((g > 140) & green).mean())


def player_hp(norm_rgb):
    """Доля своего HP (0..1) по красной полосе снизу или None, если полосы не видно."""
    h = norm_rgb.shape[0]
    row = norm_rgb[h - PLAYER_HP_FROM_BOTTOM, PLAYER_HP_X[0]:PLAYER_HP_X[1]].astype(int)
    r, g = row[:, 0], row[:, 1]
    red = (r > 130) & (r - g > 60)
    dark_red = (r > 40) & (r - g > 25)
    if (red | dark_red).mean() < 0.7:
        return None
    return float(red.mean())


def read_rewards(norm_rgb, tpl, threshold=0.8):
    """Награды из окна «Raid Summary»: {"crystals": n, "chests": n}; None — карточка не найдена,
    -1 — карточка есть, но число не прочиталось."""
    g = cv2.cvtColor(norm_rgb, cv2.COLOR_RGB2GRAY)
    h = g.shape[0]
    y0, y1 = int(REWARD_BAND[0] * h), int(REWARD_BAND[1] * h)
    out = {}
    for key, name in (("crystals", "crystal"), ("chests", "chest")):
        t = tpl[name]
        _, score, _, (mx, my) = cv2.minMaxLoc(cv2.matchTemplate(g[y0:y1], t, cv2.TM_CCOEFF_NORMED))
        if score < threshold:
            out[key] = None
            continue
        cx, cy = mx + t.shape[1] // 2, y0 + my + t.shape[0] // 2
        # число под иконкой: при иконке в (762, 545) текст «x360» лежит в x 714..806, y 594..612
        n = digits.read_number(norm_rgb[cy + 49:cy + 67, cx - 48:cx + 44])
        out[key] = -1 if n is None else n
    return out


class Stats:
    """Рейды, кристаллы и сундуки: за текущую сессию (с запуска скрипта) и за всё время.

    Последняя сессия тоже пишется в файл — её показывает бот, даже когда скрипт не запущен."""

    def __init__(self):
        self.data = {"raids": 0, "crystals": 0, "chests": 0, "history": [], "session": None}
        if STATS_PATH.exists():
            try:
                self.data.update(json.loads(STATS_PATH.read_text(encoding="utf-8")))
            except Exception:
                pass
        self.session = {"start": datetime.now().isoformat(timespec="seconds"),
                        "raids": 0, "crystals": 0, "chests": 0}

    def add(self, sec, crystals, chests):
        for d in (self.data, self.session):
            d["raids"] += 1
            d["crystals"] += max(crystals or 0, 0)
            d["chests"] += max(chests or 0, 0)
        self.data["session"] = self.session
        self.data["history"] = (self.data["history"] + [{
            "end": datetime.now().isoformat(timespec="seconds"), "sec": sec,
            "crystals": crystals, "chests": chests}])[-1000:]
        STATS_PATH.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")

    def text(self, multiline=False, live=True):
        """live=False — показать последнюю сохранённую сессию (скрипт сейчас не запущен)."""
        d = self.data
        s = self.session if live else (d.get("session") or {"raids": 0, "crystals": 0, "chests": 0})
        secs = [x["sec"] for x in d["history"][-20:] if x.get("sec")]
        avg = f"{int(sum(secs) / len(secs))} с" if secs else "-"
        if not multiline:
            return (f"сессия: рейдов {s['raids']}, кристаллов {s['crystals']}, сундуков {s['chests']}; "
                    f"всего: рейдов {d['raids']}, кристаллов {d['crystals']}, сундуков {d['chests']}")
        since = s.get("start", "")[:16].replace("T", " ")
        return (f"Сессия{' с ' + since if since else ''}:\n"
                f"  рейдов {s['raids']}\n  кристаллов {s['crystals']}\n  сундуков {s['chests']}\n"
                f"Всего:\n  рейдов {d['raids']}\n  кристаллов {d['crystals']}\n  сундуков {d['chests']}\n"
                f"Средний рейд (последние 20): {avg}")


class MahitoRaid(ZeninRaid):
    """Кнопки, уведомления, выбор техники и клики берутся из рейда Зенин."""

    def __init__(self, args):
        super().__init__(args)
        self.mtpl = load_mahito_templates()
        self.stats = Stats()
        self.held = set()
        self.lobby_since = None     # когда нажали Retry (до Ready маркеры не трогаем)
        self.groups_killed = 0
        self.stage, self.stage_since, self.failed = None, 0.0, False
        self.nav = None              # текущая цель фазы НПС (см. navigate)
        self.rewards = None          # награды этого рейда (считываются на окне Close)
        self.boss_logged = None      # последний залогированный десяток процентов HP босса
        self.dodge_at = 0.0
        self.phase = None
        self.hwnd = None
        self.pending_sec = None      # длительность рейда, пока не дошли до окна наград

    def log(self, msg):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d} {line}\n")

    def set_phase(self, name, text):
        if self.phase != name:
            self.phase = name
            self.log(text)
        # этап для таймера: бой с боссом то и дело прерывается «босса не видно» — это один этап,
        # как и ожидание босса после зачистки (если игра его не заспавнила, там и зависнет)
        stage = "boss" if name in ("boss", "idle") else name
        if stage != self.stage:
            self.stage, self.stage_since = stage, time.time()

    STAGE_NAMES = {"nav2": "зачистка первой группы", "nav1": "зачистка второй группы",
                   "boss": "ожидание / бой с боссом"}

    def check_stage(self):
        """Один этап рейда тянется дольше stage_max с (игра не заспавнила босса, персонаж застрял) —
        респавн Esc → R → Enter. Игра показывает «Raid Summary: Failure» с Next и Retry сверху,
        дальше всё как обычно; проваленный рейд в статистику не идёт. True — респавн сделан."""
        if self.stage is None or self.raid_start is None:
            return False
        took = time.time() - self.stage_since
        if took < self.a.stage_max:
            return False
        name = self.STAGE_NAMES.get(self.stage, self.stage)
        self.release()
        self.log(f"этап «{name}» идёт {took:.0f} с — респавн (рейд будет проваленным)")
        self.notify(f"⚠️ Махито: этап «{name}» завис на {took:.0f} с, делаю респавн", with_screen=True)
        for key in ("esc", "r", "enter"):
            pydirectinput.press(key)
            time.sleep(0.6)
        self.failed = True
        self.nav = None
        self.phase = self.stage = None
        time.sleep(3)
        return True

    # ---- клавиши движения

    def hold(self, keys):
        keys = set(keys)
        for k in self.held - keys:
            pydirectinput.keyUp(k)
        for k in keys - self.held:
            pydirectinput.keyDown(k)
        self.held = keys

    def release(self):
        self.hold(())

    def toggle(self):
        super().toggle()
        if not self.running.is_set():
            self.release()

    # ---- кнопки и награды

    def on_button(self, hwnd, name, fx, fy, score):
        now = time.time()
        if name == "next" and self.raid_start is not None and now >= self.cooldown.get(name, 0):
            if self.failed:
                # после респавна «Failure»: не считаем рейд пройденным и наград не ждём
                self.log(f"рейд провален (респавн) за {int(now - self.raid_start)} с")
                self.raid_start = None
                self.rewards = {}
            else:
                self.pending_sec = int(now - self.raid_start)
        if name in ("ready", "retry") and now >= self.cooldown.get(name, 0):
            self.nav = None
            self.rewards = None
            self.boss_logged = None
            self.phase = self.stage = None
            self.failed = False
            self.groups_killed = 0
            self.lobby_since = now if name == "retry" else None
        super().on_button(hwnd, name, fx, fy, score)

    def on_close(self, img):
        """Окно наград: один раз за рейд прочитать кристаллы и сундуки и записать в статистику."""
        if self.rewards is not None:
            return
        r = read_rewards(img, self.mtpl)
        self.rewards = r
        bad = [k for k, v in r.items() if v is None or v < 0]
        if bad:
            UNREAD_DIR.mkdir(parents=True, exist_ok=True)
            p = UNREAD_DIR / f"rewards_{datetime.now():%Y%m%d_%H%M%S}.png"
            cv2.imwrite(str(p), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
            self.log(f"не прочитал награды {', '.join(bad)} — снимок в {p.relative_to(BASE)}")
            self.notify(f"⚠️ Махито: не прочитал награды ({', '.join(bad)}), в статистику не попали. "
                        f"Снимок сохранён в {p.relative_to(BASE)}", with_screen=True)
        sec = self.pending_sec
        self.pending_sec = None
        self.stats.add(sec, r["crystals"], r["chests"])
        fmt = lambda v: "?" if v is None or v < 0 else str(v)
        self.log(f"награды: кристаллы {fmt(r['crystals'])}, сундуки {fmt(r['chests'])} | {self.stats.text()}")

    # ---- фаза НПС: дойти до маркера и убить группу

    def steer(self, x, y, dist):
        """Клавиши, ведущие к маркеру в точке (x, y) экрана; True — уже на месте
        (расстояние дважды подряд не больше arrive_m, чтобы одно ошибочное чтение не останавливало)."""
        a = self.a
        nav = self.nav
        if dist is not None:
            nav["near"] = nav.get("near", 0) + 1 if dist <= a.arrive_m else 0
        if nav.get("near", 0) >= 2:
            return set(), True
        if x < 0.05:            # маркер за краем экрана (сбоку или сзади) — вбок
            return {"a"}, False
        if x > 0.95:
            return {"d"}, False
        keys = set()
        if x < 0.5 - a.deadband:
            keys.add("a")
        elif x > 0.5 + a.deadband:
            keys.add("d")
        keys.add("s" if y > 0.62 else "w")   # маркер ниже персонажа — он позади
        return keys, False

    def unstick(self, keys, dist, now):
        """Расстояние не уменьшилось на 3 м за stuck_sec с — упёрся во что-то: на detour_sec с
        держим обходную комбинацию (вбок+назад, вбок+вперёд, назад, вперёд по очереди)."""
        a = self.a
        nav = self.nav
        if now < nav.get("detour_until", 0):
            return nav["detour_keys"]
        if dist is not None and (nav.get("best") is None or dist < nav["best"] - 3):
            nav["best"], nav["progress_at"] = dist, now
        if now - nav.setdefault("progress_at", now) < a.stuck_sec:
            return keys
        side = (keys & {"a", "d"}) or {random.choice("ad")}
        options = [side | {"s"}, side | {"w"}, {"s"}, {"w"}]
        i = nav.get("detours", 0)
        dk = options[i % len(options)]
        nav.update(detours=i + 1, detour_keys=dk, detour_until=now + a.detour_sec,
                   best=None, progress_at=now + a.detour_sec)
        self.log(f"застрял ({dist if dist is not None else '?'} м) — обхожу: "
                 f"{'+'.join(sorted(dk)).upper()} {a.detour_sec:.1f} с")
        return dk

    def navigate(self, markers):
        now = time.time()
        a = self.a
        if self.nav is None:
            # по очереди: сначала самый левый маркер
            x, y, d = min(markers, key=lambda m: m[0])
            self.nav = {"pos": (x, y), "count": len(markers), "state": "walk", "since": now,
                        "fewer_since": None, "last_f": 0.0, "jiggles": 0, "last_log": 0.0}
            self.set_phase(f"nav{len(markers)}", f"фаза НПС: маркеров {len(markers)}, иду к "
                           f"{'левому' if len(markers) > 1 else 'последнему'}"
                           + (f" ({d} м)" if d is not None else ""))
        nav = self.nav

        # маркеров стало меньше и так держится — группа убита
        fewer = len(markers) < nav["count"]
        if fewer:
            if nav["fewer_since"] is None:
                nav["fewer_since"] = now
            elif now - nav["fewer_since"] >= 1.5:
                self.release()
                self.log(f"маркер погас — группа убита за {int(now - nav['since'])} с")
                self.groups_killed += 1
                self.nav = None
                return
        else:
            nav["fewer_since"] = None

        # текущая цель — маркер, ближайший к прошлому положению цели. Если своей цели в кадре нет,
        # а оставшийся маркер далеко — это другая группа: не переключаемся, идём как шли
        px, py = nav["pos"]
        x, y, dist = min(markers, key=lambda m: (m[0] - px) ** 2 + (m[1] - py) ** 2)
        if fewer and (x - px) ** 2 + (y - py) ** 2 > 0.3 ** 2:
            time.sleep(0.05)
            return
        nav["pos"] = (x, y)

        if nav["state"] == "walk":
            keys, arrived = self.steer(x, y, dist)
            # издалека бить бесполезно: «атакую отсюда» только если маркер близко или расстояние не читается
            if (not arrived and now - nav["since"] > a.walk_max
                    and (dist is None or dist <= a.arrive_m + 20)):
                self.log(f"иду к маркеру дольше {a.walk_max:.0f} с — атакую отсюда")
                arrived = True
            if not arrived:
                keys = self.unstick(keys, dist, now)
            if arrived:
                self.release()
                self.log("у маркера" + (f" ({dist} м)" if dist is not None else "") + f" — жму {a.fight_key.upper()}")
                nav.update(state="fight", fight_since=now, last_f=0.0, far=0)
            else:
                self.hold(keys)
                # пол неровный — на ходу серия прыжков: jump_count нажатий пробела через jump_gap с,
                # потом пауза jump_pause с (сплошные прыжки уносят вверх и не дают набрать скорость)
                if now - nav.get("last_jump", 0) >= a.jump_pause:
                    for i in range(a.jump_count):
                        if i:
                            time.sleep(a.jump_gap)
                        pydirectinput.press("space")
                    nav["last_jump"] = time.time()
                if now - nav["last_log"] > 3:
                    nav["last_log"] = now
                    self.log(f"  маркер ({x:.2f}, {y:.2f})" + (f" {dist} м" if dist is not None else "")
                             + f" → {'+'.join(sorted(keys)).upper()}")
                time.sleep(0.05)
                return

        # на месте: бьём, ждём, пока маркер погаснет
        if dist is not None and dist > a.arrive_m + 10:
            nav["far"] = nav.get("far", 0) + 1
            if nav["far"] >= 3:   # три чтения подряд — не случайная ошибка
                self.log(f"отнесло от маркера ({dist} м) — подхожу снова")
                nav.update(state="walk", since=now, near=0, best=None, progress_at=now, detour_until=0)
                return
        elif dist is not None:
            nav["far"] = 0
        if now - nav["last_f"] >= a.fight_every:
            nav["last_f"] = now
            pydirectinput.press(a.fight_key)
        if now - nav["fight_since"] > a.kill_wait:
            nav["jiggles"] += 1
            if nav["jiggles"] > a.jiggles:
                self.log("маркер не гаснет — снова иду к нему")
                nav.update(state="walk", since=now, jiggles=0, near=0, best=None, progress_at=now, detour_until=0)
                return
            key = random.choice(MOVE_KEYS)
            self.log(f"маркер не гас {a.kill_wait:.0f} с — сдвигаюсь ({key.upper()}) и бью снова")
            self.hold({key})
            pydirectinput.press("space")
            time.sleep(a.jiggle_sec)
            self.release()
            pydirectinput.press(a.fight_key)
            nav.update(fight_since=time.time(), last_f=time.time())
        time.sleep(0.2)

    # ---- фазы босса

    def fight_boss(self, hp, img):
        self.nav = None
        self.release()
        self.set_phase("boss", "босс Махито — бью способностями")
        tenth = int(hp * 10)
        if self.boss_logged != tenth:
            self.boss_logged = tenth
            self.log(f"  HP босса {hp:.0%}")
        mine = player_hp(img)
        if mine is not None and mine < self.a.low_hp and time.time() - self.dodge_at > 4:
            self.dodge_at = time.time()
            self.log(f"мало HP ({mine:.0%}) — уворот {self.a.dodge_key.upper()}")
            pydirectinput.press(self.a.dodge_key)
            time.sleep(0.3)
        self.abilities()

    # ---- Telegram

    PHASES = {"boss": "бой с боссом", "idle": "переход / отсчёт"}

    def status_text(self):
        state = "работает" if self.running.is_set() else "на паузе"
        ph = self.PHASES.get(self.phase, "фаза НПС" if (self.phase or "").startswith("nav") else "-")
        cur = f"{int(time.time() - self.raid_start)} с" if self.raid_start else "-"
        return (f"Махито: {state}\nФаза: {ph}\nТекущий рейд идёт: {cur}\n"
                f"Окно Roblox: {'есть' if find_window(self.a.window) else 'НЕ НАЙДЕНО'}")

    def tg_commands(self):
        from winutil import screenshot_png

        def toggle():
            self.toggle()
            return "Запущен" if self.running.is_set() else "На паузе"

        return [
            tgbot.Command("stats", "📊 Заработано", lambda: self.stats.text(multiline=True)),
            tgbot.Command("status", "ℹ️ Состояние", self.status_text),
            tgbot.Command("toggle", "⏯ Старт / пауза", toggle),
            tgbot.Command("screen", "🖼 Скриншот", lambda: (self.status_text(), screenshot_png())),
        ]

    # ---- главный цикл

    def loop(self):
        while True:
            if not self.running.is_set():
                self.release()
                self.running.wait()
            hwnd = self.hwnd = find_window(self.a.window)
            if not hwnd or user32.GetForegroundWindow() != hwnd:
                self.release()
                time.sleep(1)
                continue
            try:
                raw = self.grab(hwnd)
                img = vision.normalize(raw)
                found = find_button(raw, self.tpl, self.a.threshold)
            except Exception as e:
                self.log(f"ошибка снимка экрана: {e}")
                time.sleep(1)
                continue

            if found:
                self.release()
                self.nav = None
                if found[0] == "close":
                    self.on_close(img)
                self.on_button(hwnd, *found)
                time.sleep(0.5)
                continue

            if not self.stuck_sent and time.time() - self.last_button > self.a.stuck_min * 60:
                self.stuck_sent = True
                self.log(f"больше {self.a.stuck_min} мин не видно ни одной кнопки рейда — похоже, застрял")
                self.notify(f"⚠️ Махито: {self.a.stuck_min} мин не видно кнопок рейда, проверь игру",
                            with_screen=True)

            if self.check_stage():
                continue

            if technique_equipped(raw) is False and time.time() - self.equip_at > 3:
                self.release()
                self.equip_at = time.time()
                self.log(f"техника не выбрана — жму {self.a.equip_key}")
                pydirectinput.press(self.a.equip_key)
                time.sleep(0.3)
                continue

            hp = boss_hp(img)
            if hp is not None:
                self.fight_boss(hp, img)
                continue

            # после Retry маркеры видны ещё в лобби, до Ready — тогда стоим и ждём кнопку
            # (не дольше минуты: вдруг Ready пропустили, а рейд уже идёт)
            if self.lobby_since is not None and time.time() - self.lobby_since < 60:
                self.release()
                time.sleep(0.3)
                continue

            markers = find_markers(img, self.mtpl)
            # на старте рейда должны быть видны обе группы: один маркер в первые секунды —
            # вторую закрывает интерфейс, подождать
            if (markers and self.nav is None and self.groups_killed == 0 and len(markers) < 2
                    and self.raid_start is not None and time.time() - self.raid_start < 5):
                time.sleep(0.2)
                continue
            if markers:
                self.navigate(markers)
                continue

            # переход между фазами / отсчёт: стоим и жмём способности
            self.release()
            if self.nav is None:
                self.set_phase("idle", "маркеров и босса не видно — жму способности на месте")
            self.abilities()


def test_mode(files):
    tpl = load_templates()
    mtpl = load_mahito_templates()
    for f in files:
        bgr = cv2.imread(f)
        if bgr is None:
            continue
        raw = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        img = vision.normalize(raw)
        parts = []
        b = find_button(raw, tpl, 0.75)
        if b:
            parts.append(f"кнопка {b[0]} ({b[3]:.2f})")
            if b[0] == "close":
                parts.append(f"награды {read_rewards(img, mtpl)}")
        hp = boss_hp(img)
        if hp is not None:
            parts.append(f"босс {hp:.0%}")
        m = find_markers(img, mtpl)
        if m:
            parts.append("маркеры " + ", ".join(f"({x:.2f},{y:.2f}) {d if d is not None else '?'} м" for x, y, d in m))
        mine = player_hp(img)
        parts.append(f"моё HP {mine:.0%}" if mine is not None else "моего HP не видно")
        parts.append("техника " + {True: "выбрана", False: "НЕ выбрана", None: "?"}[technique_equipped(raw)])
        print(f"{Path(f).name}: " + "; ".join(parts))


def main():
    ap = argparse.ArgumentParser(description="Рейд Махито: маркеры НПС → босс → награды → Retry")
    ap.add_argument("--keys", nargs="+", default=["r", "f", "x", "c"], help="способности по кругу против босса")
    ap.add_argument("--fight-key", default="f", help="способность против группы НПС")
    ap.add_argument("--fight-every", type=float, default=3.0, help="как часто жать её у маркера, с")
    ap.add_argument("--equip-key", default="1", help="клавиша выбора техники (слот 1)")
    ap.add_argument("--key-gap", type=float, default=0.25, help="пауза между клавишами способностей, с")
    ap.add_argument("--threshold", type=float, default=0.75, help="порог совпадения шаблона кнопки")
    ap.add_argument("--arrive-m", type=int, default=8, help="на каком расстоянии до маркера (м) считать, что дошёл")
    ap.add_argument("--deadband", type=float, default=0.06, help="допуск по горизонтали, в пределах которого не стрейфим")
    ap.add_argument("--walk-max", type=float, default=40, help="больше скольких секунд не идти к одному маркеру")
    ap.add_argument("--kill-wait", type=float, default=10, help="сколько секунд ждать, пока маркер погаснет")
    ap.add_argument("--jiggles", type=int, default=4, help="сколько раз сдвигаться, прежде чем снова идти к маркеру")
    ap.add_argument("--jiggle-sec", type=float, default=0.7, help="на сколько секунд сдвигаться")
    ap.add_argument("--jump-count", type=int, default=3, help="сколько раз жать пробел в серии по пути к маркеру")
    ap.add_argument("--jump-gap", type=float, default=0.1, help="пауза между нажатиями в серии, с")
    ap.add_argument("--jump-pause", type=float, default=1.0, help="пауза между сериями, с")
    ap.add_argument("--stuck-sec", type=float, default=4.0, help="расстояние до маркера не уменьшается столько секунд — застрял, обходить")
    ap.add_argument("--detour-sec", type=float, default=1.5, help="сколько секунд держать обходную комбинацию клавиш")
    ap.add_argument("--stage-max", type=float, default=120, help="этап рейда дольше стольких секунд — респавн (Esc, R, Enter)")
    ap.add_argument("--low-hp", type=float, default=0.35, help="доля своего HP, ниже которой уворачиваться от босса")
    ap.add_argument("--dodge-key", default="q")
    ap.add_argument("--stuck-min", type=float, default=10, help="через сколько минут без кнопок слать «застрял»")
    ap.add_argument("--window", default="Roblox")
    ap.add_argument("--on", action="store_true", help="начать сразу, без F8")
    ap.add_argument("--stats", action="store_true", help="показать статистику и выйти")
    ap.add_argument("--test", nargs="*", help="проверить распознавание на снимках и выйти")
    args = ap.parse_args()

    if args.test is not None:
        test_mode(args.test)
        return
    if args.stats:
        print(Stats().text(multiline=True, live=False))
        return

    z = MahitoRaid(args)
    threading.Thread(target=z.loop, daemon=True).start()
    keyboard.add_hotkey(KEY_TOGGLE, z.toggle)
    tgbot.start(z.cfg, "mahito", z.tg_commands(), z.log)
    z.log(f"рейд Махито: {KEY_TOGGLE.upper()} — старт/пауза, {KEY_EXIT.title()} — выход | {z.stats.text()}")
    if args.on:
        z.toggle()
    keyboard.wait(KEY_EXIT)
    z.running.clear()
    z.release()
    z.log(f"выход | {z.stats.text()}")


if __name__ == "__main__":
    main()
