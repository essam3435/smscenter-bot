"""Session manager bot: a single-admin Telegram bot that manages Telethon accounts you already control.

Import an account by uploading its .session file, a .zip of .session files, or by logging in with its phone
number (code entered with number buttons - Telegram cancels a login code the moment it is sent as a text
message from that account, so typing it is never safe here). For each imported account: see its active
Telegram sessions (devices) and end one or all of them, check the login is still valid, relay the codes
Telegram sends it (the "Telegram" service chat) to the admin, export its session file, remove it from this
bot, or log it out everywhere.

Independent of smscenter_price_bot.py: its own token, its own state file, its own session files.
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import html
import json
import logging
import os
import re
import sys
import tempfile
import time
import zipfile
from typing import Any

log = logging.getLogger("sessionmgr")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

STATE_PATH = os.environ.get("SESSIONBOT_STATE") or os.path.join(HERE, ".sessionbot_state.json")
SESS_DIR = os.environ.get("SESSIONBOT_SESSIONS_DIR") or os.path.join(HERE, "sessionbot_sessions")
LOGIN_TTL = 300                      # a half-finished phone login is forgotten after this many seconds
TG_SERVICE_CHAT = 777000             # Telegram's own "Telegram" service notifications account

KB = list[list[tuple[str, str]]]
Reply = tuple[str, "KB | None"]


# ── small helpers (sealing secrets, windows DPAPI) - same idea as smscenter_price_bot.py, written fresh ──
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.c_void_p)]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("dpapi is windows-only")
    inp = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.c_void_p))
    out = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("dpapi call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def seal(text: str) -> str:
    if not text:
        return ""
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(_dpapi(text.encode(), True)).decode()
    return "plain:" + text


def unseal(blob: str) -> str:
    if not blob:
        return ""
    try:
        if blob.startswith("dpapi:"):
            return _dpapi(base64.b64decode(blob[6:]), False).decode()
        return blob[6:] if blob.startswith("plain:") else blob
    except Exception:                                                    # noqa: BLE001
        return ""


def _admin_id() -> int | None:
    env = os.environ.get("SESSIONBOT_ADMIN_ID", "").strip()
    return int(env) if env.isdigit() else None


def creds() -> tuple[int, str] | None:
    """(api_id, api_hash) - the same Telegram credentials smscenter-bot's checker uses (same repo secrets).
    GitHub's secret editor can leave a BOM on a pasted value, which plain .strip() does not remove."""
    api_id, api_hash = (os.environ.get(k, "").strip().lstrip("\ufeff").strip() for k in ("TG_API_ID", "TG_API_HASH"))
    return (int(api_id), api_hash) if api_id.isdigit() and api_hash else None


def ago(t) -> str:
    """t: a Unix timestamp, or a datetime (Telethon returns date fields - date_active, date_created - as datetime objects, not floats)."""
    epoch = t.timestamp() if hasattr(t, "timestamp") else float(t)
    d = max(0, int(time.time() - epoch))
    return f"{d}s ago" if d < 60 else f"{d // 60}m ago" if d < 3600 else f"{d // 3600}h ago" if d < 86400 else f"{d // 86400}d ago"


def pre(lines: list[str]) -> str:
    return "<pre>" + html.escape("\n".join(lines)) + "</pre>" if lines else ""


class Store:
    def __init__(self, path: str):
        self.path = path
        self.d: dict[str, Any] = {"admin": None, "token": "", "accounts": {}}       # accounts: phone -> {"watch": bool, "added_at": t, "name": str}
        try:
            with open(path, encoding="utf-8") as f:
                self.d.update(json.load(f))
        except (OSError, ValueError):
            pass
        admin = _admin_id()
        if admin:
            self.d["admin"] = admin

    @property
    def token(self) -> str:
        env = os.environ.get("SESSIONBOT_TOKEN", "").strip()
        if env:
            return env
        if self.d.get("token"):
            return unseal(self.d["token"])
        try:                                                              # fallback: a committed sessionbot_token.json (no secret needed)
            with open(os.path.join(HERE, "sessionbot_token.json"), encoding="utf-8") as f:
                return str(json.load(f).get("token") or "").strip()
        except (OSError, ValueError):
            return ""

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)


# ── the manager: one TelegramClient per account that is being watched, short-lived ones otherwise ───────
class Manager:
    def __init__(self, store: Store, bot=None):
        self.store, self.bot = store, bot
        self.live: dict[str, Any] = {}             # phone -> connected TelegramClient (kept open while OTP-watch is on)
        self.watch_tasks: dict[str, asyncio.Task] = {}
        self.login: dict | None = None             # the one in-progress phone+code login (single admin)
        self.login_timer: asyncio.TimerHandle | None = None
        os.makedirs(SESS_DIR, exist_ok=True)

    def base(self, phone: str) -> str:
        return os.path.join(SESS_DIR, re.sub(r"[^0-9A-Za-z+_-]", "", phone) or phone)      # keep letters too: the id{n} fallback for phone-less accounts has no digits-only clash

    def allowed(self, uid: int) -> bool:
        return self.store.d["admin"] == uid

    # -- connections
    async def open(self, phone: str):
        from telethon import TelegramClient
        cr = creds()
        if not cr:
            raise RuntimeError("No TG_API_ID / TG_API_HASH configured.")
        c = TelegramClient(self.base(phone), cr[0], cr[1])
        await c.connect()
        return c

    async def client_for(self, phone: str):
        """A connected client for one-off calls: the live watcher connection if there is one, else a fresh one the caller must close."""
        if phone in self.live:
            return self.live[phone], False
        return await self.open(phone), True

    async def start_watch(self, phone: str) -> None:
        if phone in self.watch_tasks and not self.watch_tasks[phone].done():
            return
        self.watch_tasks[phone] = asyncio.create_task(self._watch_loop(phone))

    async def stop_watch(self, phone: str) -> None:
        t = self.watch_tasks.pop(phone, None)
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):                  # noqa: BLE001
                pass
        c = self.live.pop(phone, None)
        if c is not None:
            try:
                await c.disconnect()
            except Exception:                                            # noqa: BLE001
                pass

    async def _watch_loop(self, phone: str) -> None:
        from telethon import events
        try:
            c = await self.open(phone)
        except Exception as e:                                           # noqa: BLE001
            await self.notify(f"⚠️ Could not start the OTP watch for <code>{html.escape(phone)}</code>: {html.escape(type(e).__name__)}.")
            self.store.d["accounts"].setdefault(phone, {})["watch"] = False
            self.store.save()
            return
        self.live[phone] = c
        name = self.store.d["accounts"].get(phone, {}).get("name") or phone

        @c.on(events.NewMessage(chats=TG_SERVICE_CHAT))
        async def _on_msg(ev):                                           # noqa: ANN001
            text = (ev.raw_text or "").strip()
            if text:
                await self.notify(f"📩 <b>{html.escape(name)}</b> ({html.escape(phone)})\n{html.escape(text)}")

        try:
            await c.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as e:                                           # noqa: BLE001
            log.warning("watch loop for %s ended: %s", phone, type(e).__name__)
        finally:
            self.live.pop(phone, None)

    async def notify(self, text: str) -> None:
        if self.bot and self.store.d["admin"]:
            try:
                await self.bot.send_message(self.store.d["admin"], text, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:                                            # noqa: BLE001
                pass

    async def resume(self) -> None:
        for phone, acc in self.store.d["accounts"].items():
            if acc.get("watch") and os.path.exists(self.base(phone) + ".session"):
                await self.start_watch(phone)

    async def shutdown(self) -> None:
        for phone in list(self.watch_tasks):
            await self.stop_watch(phone)

    # -- import
    async def import_file(self, local_path: str) -> tuple[str | None, str]:
        """local_path: a downloaded .session file (any name). -> (phone or None, message)."""
        base = local_path[:-len(".session")] if local_path.endswith(".session") else local_path
        try:
            from telethon import TelegramClient
            cr = creds()
            if not cr:
                return None, "No TG_API_ID / TG_API_HASH configured."
            c = TelegramClient(base, cr[0], cr[1])
            await c.connect()
            if not await c.is_user_authorized():
                await c.disconnect()
                return None, "That session isn't logged in (expired or never completed)."
            me = await c.get_me()
            await c.disconnect()
        except Exception as e:                                           # noqa: BLE001
            return None, f"Not a usable Telethon session ({html.escape(type(e).__name__)})."
        phone = "+" + re.sub(r"\D", "", str(me.phone or "")) if me.phone else f"id{me.id}"
        dest = self.base(phone)
        existed = os.path.exists(dest + ".session")
        for ext in (".session", ".session-journal"):
            try:
                if os.path.exists(base + ext):
                    os.replace(base + ext, dest + ext)
            except OSError:
                pass
        who = (me.first_name or "") + (f" (@{me.username})" if me.username else "")
        self.store.d["accounts"].setdefault(phone, {})["added_at"] = self.store.d["accounts"].get(phone, {}).get("added_at") or time.time()
        self.store.d["accounts"][phone]["name"] = who.strip() or phone
        self.store.save()
        return phone, f"{'Updated' if existed else 'Added'} <b>{html.escape(who.strip() or phone)}</b> ({html.escape(phone)})."

    async def import_zip(self, zpath: str) -> list[str]:
        out = []
        with tempfile.TemporaryDirectory() as td:
            try:
                with zipfile.ZipFile(zpath) as z:
                    names = [n for n in z.namelist() if n.lower().endswith(".session")]
                    if not names:
                        return ["No .session file found in the zip."]
                    for n in names:
                        extracted = z.extract(n, td)
                        base = os.path.join(td, os.path.basename(n))[:-len(".session")]
                        if extracted != base + ".session":
                            try:
                                os.replace(extracted, base + ".session")
                            except OSError:
                                base = extracted[:-len(".session")]
                        _phone, msg = await self.import_file(base + ".session")
                        out.append(msg)
            except zipfile.BadZipFile:
                return ["That doesn't look like a valid zip file."]
        return out

    # -- per-account actions
    async def validate(self, phone: str) -> str:
        from telethon.errors import AuthKeyUnregisteredError, UserDeactivatedBanError, UserDeactivatedError
        try:
            c, mine = await self.client_for(phone)
        except Exception as e:                                           # noqa: BLE001
            return f"❌ Couldn't connect ({html.escape(type(e).__name__)})."
        try:
            if not await c.is_user_authorized():
                return "❌ Not logged in any more."
            me = await c.get_me()
            who = (me.first_name or "") + (f" (@{me.username})" if me.username else "")
            return f"✅ Still logged in as <b>{html.escape(who.strip())}</b>."
        except AuthKeyUnregisteredError:
            return "❌ Logged out on Telegram's side (auth key no longer valid)."
        except (UserDeactivatedError, UserDeactivatedBanError):
            return "❌ This account is deactivated or banned."
        except Exception as e:                                           # noqa: BLE001
            return f"❌ Couldn't check it: {html.escape(type(e).__name__)}."
        finally:
            if mine:
                await c.disconnect()

    async def sessions_of(self, phone: str) -> tuple[list, str | None]:
        """-> (list of telethon Authorization objects, error or None)."""
        from telethon.tl.functions.account import GetAuthorizationsRequest
        try:
            c, mine = await self.client_for(phone)
        except Exception as e:                                           # noqa: BLE001
            return [], type(e).__name__
        try:
            res = await c(GetAuthorizationsRequest())
            return list(res.authorizations), None
        except Exception as e:                                           # noqa: BLE001
            return [], type(e).__name__
        finally:
            if mine:
                await c.disconnect()

    async def end_session(self, phone: str, auth_hash: int) -> str:
        from telethon.tl.functions.account import ResetAuthorizationRequest
        try:
            c, mine = await self.client_for(phone)
        except Exception as e:                                           # noqa: BLE001
            return f"❌ Couldn't connect ({html.escape(type(e).__name__)})."
        try:
            await c(ResetAuthorizationRequest(hash=auth_hash))
            return "✅ That device is logged out."
        except Exception as e:                                           # noqa: BLE001
            return f"❌ Couldn't end it: {html.escape(type(e).__name__)}."
        finally:
            if mine:
                await c.disconnect()

    async def end_all_others(self, phone: str) -> str:
        from telethon.errors import FloodWaitError
        from telethon.tl.functions.auth import ResetAuthorizationsRequest
        try:
            c, mine = await self.client_for(phone)
        except Exception as e:                                           # noqa: BLE001
            return f"❌ Couldn't connect ({html.escape(type(e).__name__)})."
        try:
            await c(ResetAuthorizationsRequest())
            return "✅ Every other device is logged out. Only this bot's session remains."
        except FloodWaitError as e:
            return f"⏳ Telegram limits this to about once a day - wait {e.seconds}s and try again."
        except Exception as e:                                           # noqa: BLE001
            return f"❌ Couldn't do it: {html.escape(type(e).__name__)}."
        finally:
            if mine:
                await c.disconnect()

    async def logout(self, phone: str) -> str:
        await self.stop_watch(phone)
        try:
            c = await self.open(phone)
        except Exception as e:                                           # noqa: BLE001
            return f"❌ Couldn't connect to log it out ({html.escape(type(e).__name__)})."
        try:
            await c.log_out()
        except Exception as e:                                           # noqa: BLE001
            return f"❌ Logout failed: {html.escape(type(e).__name__)}."
        self.remove(phone)
        return "🚪 Logged out everywhere and removed from the bot."

    def remove(self, phone: str) -> None:
        for ext in (".session", ".session-journal"):
            try:
                os.remove(self.base(phone) + ext)
            except FileNotFoundError:
                pass
        self.store.d["accounts"].pop(phone, None)
        self.store.save()


# ── the bot ───────────────────────────────────────────────────────────────────────────────────────────
def _nav() -> KB:
    return [[("🏠 Menu", "m")]]


def main_screen(mgr: Manager) -> Reply:
    accs = mgr.store.d["accounts"]
    txt = (f"🔐 <b>Session manager</b>\n{len(accs)} account{'s' if len(accs) != 1 else ''} · "
           f"{sum(1 for a in accs.values() if a.get('watch'))} being watched for codes")
    kb: KB = [[("📂 Accounts", "a:l")], [("➕ Add account", "a:add")]]
    return txt, kb


def accounts_screen(mgr: Manager) -> Reply:
    accs = mgr.store.d["accounts"]
    if not accs:
        return "📂 <b>Accounts</b>\nNone yet. Add one below.", [[("➕ Add account", "a:add")], [("🏠 Menu", "m")]]
    kb: KB = []
    for phone, acc in sorted(accs.items(), key=lambda kv: -(kv[1].get("added_at") or 0)):
        dot = "📡" if acc.get("watch") else "⚪"
        kb.append([(f"{dot} {(acc.get('name') or phone)[:24]}", f"a:v:{phone}")])
    kb.append([("➕ Add account", "a:add")])
    kb.append([("🏠 Menu", "m")])
    return f"📂 <b>Accounts</b> ({len(accs)})\n<i>📡 = OTP watch on</i>", kb


def account_card(mgr: Manager, phone: str, note: str = "") -> Reply:
    acc = mgr.store.d["accounts"].get(phone)
    if acc is None:
        return "I don't manage that account any more.", [[("⬅️ Accounts", "a:l")]]
    watch = bool(acc.get("watch"))
    txt = (f"👤 <b>{html.escape(acc.get('name') or phone)}</b>\n📞 <code>{html.escape(phone)}</code>\n"
           f"Added {ago(acc.get('added_at') or time.time())}\n📡 OTP watch: {'ON' if watch else 'off'}"
           + (f"\n\n{note}" if note else ""))
    kb: KB = [[("🔁 Validate", f"a:val:{phone}"), ("📟 Sessions", f"a:s:{phone}:0")],
              [(("📡 Watch: ON" if watch else "📡 Watch: off"), f"a:w:{phone}")],
              [("📤 Export session", f"a:exp:{phone}"), ("🗑 Remove", f"a:rm:{phone}")],
              [("🚪 Logout everywhere", f"a:lo:{phone}")],
              [("⬅️ Accounts", "a:l"), ("🏠 Menu", "m")]]
    return txt, kb


def sessions_screen(phone: str, auths: list, page: int) -> Reply:
    per = 6
    pages = max(1, (len(auths) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    part = auths[page * per:(page + 1) * per]
    kb: KB = []
    for a in part:
        if getattr(a, "current", False):
            continue
        label = f"{a.device_model or a.platform or '?'} · {a.app_name or ''} · {a.country or ''} · {ago(a.date_active) if getattr(a, 'date_active', None) else ''}"
        kb.append([(f"🔴 {label[:45]}", f"a:se:{phone}:{a.hash}:{page}")])
    nav = []
    if page > 0:
        nav.append(("⬅️", f"a:s:{phone}:{page - 1}"))
    nav.append((f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("➡️", f"a:s:{phone}:{page + 1}"))
    kb.append(nav)
    if len(auths) > 1:
        kb.append([("🧹 Kill ALL other sessions", f"a:ka:{phone}")])
    kb.append([("🔄 Refresh", f"a:s:{phone}:{page}"), ("⬅️ Account", f"a:v:{phone}")])
    lines = [f"📱 {a.device_model or a.platform or '?'} ({a.app_name or '?'})" + (" · 📍 this bot" if getattr(a, "current", False) else "")
             + f"\n   {a.country or '?'} · last active {ago(a.date_active) if getattr(a, 'date_active', None) else '?'}" for a in auths]
    return f"📟 <b>Active sessions</b> ({len(auths)})\n" + pre(lines), kb


class SessionManagerBot:
    def __init__(self, mgr: Manager):
        self.mgr = mgr

    async def handle(self, uid: int, text: str) -> Reply | None:
        d = self.mgr.store.d
        text = text.strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/start" and d["admin"] is None:
            d["admin"] = uid
            self.mgr.store.save()
            return "👑 You are the admin of this bot.\n\n" + main_screen(self.mgr)[0], main_screen(self.mgr)[1]
        if not self.mgr.allowed(uid):
            return "This bot is private.", None
        if cmd in ("/start", "/menu"):
            return main_screen(self.mgr)
        if cmd == "/accounts":
            return accounts_screen(self.mgr)
        login = self.mgr.login
        if login and login.get("stage") == "phone" and not cmd.startswith("/"):
            return await self.login_phone(text)
        if login and login.get("stage") == "pw" and not cmd.startswith("/"):
            return await self.login_password(text)
        return "Use the buttons (/menu).", _nav()

    def arm_login(self) -> None:
        if self.mgr.login_timer:
            self.mgr.login_timer.cancel()
        self.mgr.login_timer = asyncio.get_running_loop().call_later(LOGIN_TTL, lambda: asyncio.create_task(self.login_expire()))

    async def login_expire(self) -> None:
        login, self.mgr.login = self.mgr.login, None
        if login and login.get("client"):
            try:
                await login["client"].disconnect()
            except Exception:                                            # noqa: BLE001
                pass
        await self.mgr.notify("⏱ The login timed out (5 min). Send /menu → ➕ Add account to start again.")

    async def drop_login(self) -> None:
        if self.mgr.login_timer:
            self.mgr.login_timer.cancel()
            self.mgr.login_timer = None
        login, self.mgr.login = self.mgr.login, None
        if login and login.get("client"):
            try:
                await login["client"].disconnect()
            except Exception:                                            # noqa: BLE001
                pass

    @staticmethod
    def keypad(extra: KB | None = None) -> KB:
        k = lambda t, d: (t, "lg:k:" + d)                                 # noqa: E731
        rows = [[k("1", "1"), k("2", "2"), k("3", "3")], [k("4", "4"), k("5", "5"), k("6", "6")],
                [k("7", "7"), k("8", "8"), k("9", "9")], [k("⌫", "b"), k("0", "0"), k("✅ Send", "ok")]]
        return rows + (extra or [[("Cancel", "lg:x")]])

    def code_prompt(self) -> str:
        n = len(self.mgr.login.get("buf", ""))
        return (f"📨 Code sent to the Telegram app on the account's other device.\n\n<b>Code:</b> {'● ' * n if n else '-'}\n"
                "Tap the digits below, then <b>✅ Send</b>. <i>Never type the code as a message: Telegram cancels any code it sees sent as text.</i>")

    async def ui(self, uid: int, data: str) -> Reply | None:
        if not self.mgr.allowed(uid):
            return None
        p = data.split(":")
        k = p[0]
        if k == "m":
            return main_screen(self.mgr)
        if k == "noop":
            return None
        if k == "a":
            return await self.ui_account(p)
        if k == "lg":
            return await self.ui_login(p)
        return main_screen(self.mgr)

    async def ui_account(self, p: list[str]) -> Reply | None:
        sub = p[1]
        if sub == "l":
            return accounts_screen(self.mgr)
        if sub == "add":
            return ("➕ <b>Add account</b>\nSend a <code>.session</code> file, or a <code>.zip</code> of several, as a document. "
                    "Or log in with the phone number."), [[("📱 Login with phone", "lg:start")], [("⬅️ Menu", "m")]]
        if sub == "v":
            return account_card(self.mgr, p[2])
        if sub == "val":
            msg = await self.mgr.validate(p[2])
            return account_card(self.mgr, p[2], msg)
        if sub == "w":
            phone = p[2]
            acc = self.mgr.store.d["accounts"].get(phone)
            if acc is None:
                return accounts_screen(self.mgr)
            acc["watch"] = not acc.get("watch")
            self.mgr.store.save()
            if acc["watch"]:
                await self.mgr.start_watch(phone)
            else:
                await self.mgr.stop_watch(phone)
            return account_card(self.mgr, phone)
        if sub == "s":
            auths, err = await self.mgr.sessions_of(p[2])
            if err:
                return account_card(self.mgr, p[2], f"❌ Couldn't read the sessions: {html.escape(err)}.")
            return sessions_screen(p[2], auths, int(p[3]))
        if sub == "se":
            msg = await self.mgr.end_session(p[2], int(p[3]))
            auths, err = await self.mgr.sessions_of(p[2])
            scr = sessions_screen(p[2], auths, int(p[4])) if not err else account_card(self.mgr, p[2])
            return msg + "\n\n" + scr[0], scr[1]
        if sub == "ka":
            msg = await self.mgr.end_all_others(p[2])
            return account_card(self.mgr, p[2], msg)
        if sub == "exp":
            return "exp", None               # handled specially by the caller (sends the file)
        if sub == "rm":
            name = (self.mgr.store.d["accounts"].get(p[2]) or {}).get("name") or p[2]
            return f"Remove <b>{html.escape(name)}</b> from this bot? Its Telegram login stays active - this only stops the bot managing it.", \
                   [[("✅ Yes, remove", f"a:rmy:{p[2]}"), ("Cancel", f"a:v:{p[2]}")]]
        if sub == "rmy":
            await self.mgr.stop_watch(p[2])
            self.mgr.remove(p[2])
            return "🗑 Removed.", accounts_screen(self.mgr)[1]
        if sub == "lo":
            name = (self.mgr.store.d["accounts"].get(p[2]) or {}).get("name") or p[2]
            return f"🚪 Log <b>{html.escape(name)}</b> out everywhere? This ends every device, including this bot's own session.", \
                   [[("✅ Yes, log out", f"a:loy:{p[2]}"), ("Cancel", f"a:v:{p[2]}")]]
        if sub == "loy":
            msg = await self.mgr.logout(p[2])
            return msg, accounts_screen(self.mgr)[1]
        return accounts_screen(self.mgr)

    async def ui_login(self, p: list[str]) -> Reply | None:
        sub = p[1] if len(p) > 1 else ""
        if sub == "x":
            await self.drop_login()
            return "Cancelled.", _nav()
        if sub == "start":
            await self.drop_login()
            if not creds():
                return "No TG_API_ID / TG_API_HASH configured for this bot.", _nav()
            self.mgr.login = {"stage": "phone", "client": None, "phone": "", "hash": "", "buf": ""}
            self.arm_login()
            return "📱 Send the phone number, international format (<code>+15551234567</code>). I delete your message right after.", [[("Cancel", "lg:x")]]
        if sub == "k" and self.mgr.login and self.mgr.login.get("stage") == "code":
            return await self.login_key(p[2])
        return main_screen(self.mgr)

    async def login_phone(self, text: str) -> Reply:
        from telethon.errors import (ApiIdInvalidError, FloodWaitError, PhoneNumberBannedError, PhoneNumberFloodError,
                                     PhoneNumberInvalidError, SendCodeUnavailableError)
        login = self.mgr.login
        self.arm_login()
        phone = "+" + re.sub(r"\D", "", text)
        if len(phone) < 8:
            return "That doesn't look like a phone number. Send it like <code>+15551234567</code>.", [[("Cancel", "lg:x")]]
        try:
            c = await self.mgr.open(phone)
            sent = await c.send_code_request(phone)
            login.update(client=c, phone=phone, hash=sent.phone_code_hash, stage="code", buf="")
            return self.code_prompt(), self.keypad()
        except FloodWaitError as e:
            await self.drop_login()
            return f"⏳ Telegram says wait {e.seconds} seconds before trying again.", _nav()
        except SendCodeUnavailableError:
            await self.drop_login()
            return "❌ Telegram has no way left to send a code to this number right now. Try later.", _nav()
        except PhoneNumberBannedError:
            await self.drop_login()
            return "❌ Telegram says this phone number is banned.", _nav()
        except PhoneNumberFloodError:
            await self.drop_login()
            return "❌ Telegram says this number asked for too many codes. Try again later.", _nav()
        except PhoneNumberInvalidError:
            await self.drop_login()
            return "❌ Telegram says that phone number is invalid.", _nav()
        except ApiIdInvalidError:
            await self.drop_login()
            return "❌ The Telegram api id/hash is invalid (TG_API_ID / TG_API_HASH).", _nav()
        except Exception as e:                                           # noqa: BLE001
            await self.drop_login()
            log.warning("login failed: %s", type(e).__name__)
            return f"❌ Login failed: {html.escape(type(e).__name__)}.", _nav()

    async def login_key(self, key: str) -> Reply:
        login = self.mgr.login
        self.arm_login()
        buf = login.get("buf", "")
        if key == "ok":
            if len(buf) < 4:
                return "Tap at least 4 digits first.\n" + self.code_prompt(), self.keypad()
            login["buf"] = ""
            return await self.finish_code(buf)
        if key == "b":
            buf = buf[:-1]
        elif key.isdigit() and len(buf) < 8:
            buf += key
        login["buf"] = buf
        return self.code_prompt(), self.keypad()

    async def finish_code(self, code: str) -> Reply:
        from telethon.errors import PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError
        login = self.mgr.login
        try:
            await login["client"].sign_in(phone=login["phone"], code=code, phone_code_hash=login["hash"])
        except SessionPasswordNeededError:
            login["stage"] = "pw"
            return "🔒 This account has 2-step verification. Send its password. I delete your message right after.", [[("Cancel", "lg:x")]]
        except PhoneCodeInvalidError:
            return "❌ Wrong code. Tap it again.\n" + self.code_prompt(), self.keypad()
        except PhoneCodeExpiredError:
            await self.drop_login()
            return "⌛ That code expired. Send /menu → ➕ Add account to start again.", _nav()
        except Exception as e:                                           # noqa: BLE001
            await self.drop_login()
            log.warning("sign-in failed: %s", type(e).__name__)
            return f"❌ Login failed: {html.escape(type(e).__name__)}.", _nav()
        return await self.finish_login()

    async def login_password(self, text: str) -> Reply:
        from telethon.errors import PasswordHashInvalidError
        login = self.mgr.login
        self.arm_login()
        try:
            await login["client"].sign_in(password=text)
        except PasswordHashInvalidError:
            return "❌ Wrong password. Send it again, or Cancel.", [[("Cancel", "lg:x")]]
        except Exception as e:                                           # noqa: BLE001
            await self.drop_login()
            log.warning("2fa failed: %s", type(e).__name__)
            return f"❌ Login failed: {html.escape(type(e).__name__)}.", _nav()
        return await self.finish_login()

    async def finish_login(self) -> Reply:
        login = self.mgr.login
        me = await login["client"].get_me()
        who = (me.first_name or "") + (f" (@{me.username})" if me.username else "")
        phone = login["phone"]
        try:
            login["client"].session.save()
        except Exception:                                                # noqa: BLE001
            pass
        await login["client"].disconnect()
        self.mgr.login, self.mgr.login_timer = None, None
        self.mgr.store.d["accounts"][phone] = {"watch": False, "added_at": time.time(), "name": who.strip() or phone}
        self.mgr.store.save()
        return f"✅ Logged in as <b>{html.escape(who.strip())}</b> ({html.escape(phone)}).", [[("👤 Open account", f"a:v:{phone}")], [("🏠 Menu", "m")]]


async def check_token(token: str) -> str | None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as h:
            r = (await h.get(f"https://api.telegram.org/bot{token}/getMe")).json()
        return r["result"]["username"] if r.get("ok") else None
    except Exception:                                                     # noqa: BLE001
        return None


async def main() -> int:
    from aiogram import Bot, Dispatcher, F
    from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

    def to_markup(kb: KB | None):
        if not kb:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in kb])

    st = Store(STATE_PATH)
    if not st.token:
        print("Set SESSIONBOT_TOKEN.")
        return 1
    if not creds():
        print("Set TG_API_ID and TG_API_HASH.")
        return 1
    bot = Bot(st.token)
    me = await bot.get_me()
    mgr = Manager(st, bot)
    sb = SessionManagerBot(mgr)
    dp = Dispatcher()

    @dp.message(F.document, F.chat.type == "private")
    async def on_doc(m: Message):
        if not mgr.allowed(m.from_user.id):
            return
        name = (m.document.file_name or "").lower()
        if not (name.endswith(".session") or name.endswith(".zip")):
            await m.answer("Send a <code>.session</code> file or a <code>.zip</code> of them.", parse_mode="HTML")
            return
        f = await bot.get_file(m.document.file_id)
        with tempfile.TemporaryDirectory() as td:
            local = os.path.join(td, os.path.basename(name))
            await bot.download_file(f.file_path, local)
            if name.endswith(".zip"):
                msgs = await mgr.import_zip(local)
                await m.answer("\n".join(msgs), parse_mode="HTML")
            else:
                _phone, msg = await mgr.import_file(local)
                await m.answer(msg, parse_mode="HTML")

    @dp.message(F.text, F.chat.type == "private")
    async def on_text(m: Message):
        res = await sb.handle(m.from_user.id, m.text)
        if mgr.login:
            try:
                await m.delete()                                          # phone / password must not stay in the chat
            except Exception:                                             # noqa: BLE001
                pass
        if res:
            await m.answer(res[0], parse_mode="HTML", disable_web_page_preview=True, reply_markup=to_markup(res[1]))

    @dp.callback_query()
    async def on_button(q: CallbackQuery):
        try:
            await q.answer()
        except Exception:                                                 # noqa: BLE001
            pass
        if q.data and q.data.startswith("a:exp:"):
            phone = q.data.split(":", 2)[2]
            path = mgr.base(phone) + ".session"
            if mgr.allowed(q.from_user.id) and os.path.exists(path):
                with open(path, "rb") as f:
                    await bot.send_document(q.from_user.id, BufferedInputFile(f.read(), os.path.basename(path)), caption="🔑 Treat this like a password.")
            return
        try:
            res = await sb.ui(q.from_user.id, q.data or "")
        except Exception as e:                                           # noqa: BLE001  (never leave a tap with no visible response)
            log.exception("button %s failed", q.data)
            res = f"❌ That didn't work: {html.escape(type(e).__name__)}.", [[("🏠 Menu", "m")]]
        try:
            if res:
                await q.message.edit_text(res[0], parse_mode="HTML", disable_web_page_preview=True, reply_markup=to_markup(res[1]))
        except Exception as e:                                            # noqa: BLE001
            if "not modified" not in str(e):
                log.warning("edit failed: %s", e)

    await bot.set_my_commands([BotCommand(command="menu", description="Main menu")])
    print(f"Running as @{me.username}.")
    try:
        if st.d["admin"]:
            run_id = os.environ.get("GITHUB_RUN_ID")
            await bot.send_message(st.d["admin"], f"Bot started. {len(st.d['accounts'])} account(s)." + (f" Run <code>{run_id}</code>" if run_id else ""), parse_mode="HTML")
    except Exception as e:                                                # noqa: BLE001
        log.warning("start notice failed: %s", e)
    await mgr.resume()
    try:
        await dp.start_polling(bot)
    finally:
        await mgr.shutdown()
        await bot.session.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nStopped.")
