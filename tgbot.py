"""Telegram-бот с выбором рейда: /menu → кнопка рейда → кнопки его команд.

Бот запускается внутри mahito_raid.py (в фоновом потоке) и работает, пока запущен макрос.

Опрашивать бота может только один процесс: если запустить два скрипта сразу, второй получит от
Telegram ошибку 409 и бот у него работать не будет (сами макросы работают).
"""

import json
import time

import requests

# порядок кнопок в главном меню
RAIDS = [("mahito", "Рейд Махито")]
TITLES = dict(RAIDS)


class Command:
    def __init__(self, cmd, label, fn):
        """fn() → текст ответа или (текст, png-байты)."""
        self.cmd, self.label, self.fn = cmd, label, fn


def offline_commands(raid_id):
    """Команды рейда, который сейчас не запущен: только то, что можно прочитать из файлов."""
    if raid_id == "mahito":
        def stats():
            from mahito_raid import Stats
            return "Махито (не запущен), последняя сессия и всего:\n" + Stats().text(multiline=True, live=False)
        return [Command("stats", "📊 Заработано", stats)]
    return []


def _api(token, method, **data):
    try:
        return requests.post(f"https://api.telegram.org/bot{token}/{method}", json=data, timeout=15).json()
    except Exception as e:
        print(f"telegram {method} error: {e}")
        return {}


def send(token, chat_id, text, img=None, keyboard=None):
    markup = {"inline_keyboard": keyboard} if keyboard else None
    try:
        if img:
            data = {"chat_id": chat_id, "caption": text[:1024]}
            if markup:
                data["reply_markup"] = json.dumps(markup)
            requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", data=data,
                          files={"photo": ("screen.png", img)}, timeout=20)
        else:
            _api(token, "sendMessage", chat_id=chat_id, text=text, **({"reply_markup": markup} if markup else {}))
    except Exception as e:
        print(f"telegram send error: {e}")


class Bot:
    def __init__(self, token, chat_id, active_id, commands, log=print):
        """active_id — какой рейд запущен в этом процессе, commands — его команды (список Command)."""
        self.token, self.allowed = token, str(chat_id or "")
        self.active_id, self.log = active_id, log
        self.commands = {rid: (commands if rid == active_id else offline_commands(rid)) for rid, _ in RAIDS}

    # ---- клавиатуры

    def menu_keyboard(self):
        return [[{"text": ("🟢 " if rid == self.active_id else "") + title, "callback_data": f"raid:{rid}"}]
                for rid, title in RAIDS]

    def raid_keyboard(self, rid):
        cmds = self.commands.get(rid, [])
        rows = [[{"text": c.label, "callback_data": f"cmd:{rid}:{c.cmd}"} for c in cmds[i:i + 2]]
                for i in range(0, len(cmds), 2)]
        return rows + [[{"text": "← К выбору рейда", "callback_data": "menu"}]]

    def raid_text(self, rid):
        state = "запущен" if rid == self.active_id else "не запущен"
        extra = "" if self.commands.get(rid) else "\nКоманд нет — запусти его скрипт."
        return f"{TITLES[rid]} — {state}.{extra}"

    # ---- обработка

    def show_menu(self, chat, msg_id=None):
        text = "Выбери рейд:"
        if msg_id:
            _api(self.token, "editMessageText", chat_id=chat, message_id=msg_id, text=text,
                 reply_markup={"inline_keyboard": self.menu_keyboard()})
        else:
            send(self.token, chat, text, keyboard=self.menu_keyboard())

    def on_callback(self, q):
        chat = str(q["message"]["chat"]["id"])
        msg_id = q["message"]["message_id"]
        data = q.get("data", "")
        _api(self.token, "answerCallbackQuery", callback_query_id=q["id"])
        if chat != self.allowed:
            return
        if data == "menu":
            self.show_menu(chat, msg_id)
        elif data.startswith("raid:"):
            rid = data[5:]
            if rid in TITLES:
                _api(self.token, "editMessageText", chat_id=chat, message_id=msg_id, text=self.raid_text(rid),
                     reply_markup={"inline_keyboard": self.raid_keyboard(rid)})
        elif data.startswith("cmd:"):
            _, rid, cmd = data.split(":", 2)
            c = next((c for c in self.commands.get(rid, []) if c.cmd == cmd), None)
            if c is None:
                send(self.token, chat, f"{TITLES.get(rid, rid)}: команда недоступна (рейд не запущен?)",
                     keyboard=self.menu_keyboard())
                return
            try:
                res = c.fn()
            except Exception as e:
                res = f"ошибка: {e}"
            text, img = res if isinstance(res, tuple) else (res, None)
            # ответ — новым сообщением, с кнопками того же рейда, чтобы жать дальше
            send(self.token, chat, text or "готово", img, keyboard=self.raid_keyboard(rid))

    def loop(self):
        _api(self.token, "setMyCommands", commands=[{"command": "menu", "description": "Выбрать рейд и команду"}])
        self.log(f"telegram бот запущен ({TITLES[self.active_id]})")
        offset = None
        conflict_logged = False
        while True:
            try:
                r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                 params={"timeout": 25, "offset": offset}, timeout=35).json()
                if r.get("error_code") == 409:
                    if not conflict_logged:
                        conflict_logged = True
                        self.log("telegram: бота уже опрашивает другой запущенный скрипт — здесь бот выключен")
                    time.sleep(30)
                    continue
                for upd in r.get("result", []):
                    offset = upd["update_id"] + 1
                    if "callback_query" in upd:
                        self.on_callback(upd["callback_query"])
                        continue
                    msg = upd.get("message") or {}
                    chat = str(msg.get("chat", {}).get("id", ""))
                    if not chat:
                        continue
                    if not self.allowed:
                        send(self.token, chat, f"Твой chat_id: {chat}\nВпиши его в config.json → "
                                               "telegram.allowed_chat_id и перезапусти макрос.")
                        continue
                    if chat == self.allowed:
                        self.show_menu(chat)   # любое сообщение (/menu, /start, текст) — меню рейдов
            except Exception as e:
                print(f"telegram poll error: {e}")
                time.sleep(5)


def start(cfg, active_id, commands, log=print):
    """Запустить бота в фоне, если в config.json есть токен. cfg — словарь config.json."""
    import threading
    tg = (cfg or {}).get("telegram") or {}
    if not tg.get("bot_token"):
        return None
    bot = Bot(tg["bot_token"], tg.get("allowed_chat_id"), active_id, commands, log)
    threading.Thread(target=bot.loop, daemon=True).start()
    return bot
