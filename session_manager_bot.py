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
CLONE_WAIT = 45.0                    # seconds to wait for the code, auto-read off the source account's own session
CLONE_TTL = 300                      # a half-finished clone (e.g. stuck on a 2FA password) is given up after this long
TG_SERVICE_CHAT = 777000             # Telegram's own "Telegram" service notifications account
HEALTH_INTERVAL = 1800.0             # how often the background health-check sweeps every account (30 min)
HEALTH_GAP = 2.0                     # seconds between each account's own check within a sweep, so a big pool doesn't open 100 connections at once
HEALTH_STALE = HEALTH_INTERVAL * 2   # a health reading older than this counts as "not checked recently" (🟡), not "healthy"
EVENT_ICON = {"import": "📥", "clone": "🧬", "kill": "🔴", "kill_all": "🧹", "logout": "🚪", "remove": "🗑",
              "watch": "📡", "code": "📩", "health": "🩺", "validate_all": "🔁", "kill_all_everywhere": "🧹", "wipe": "💣"}
WIPE_GAP = 0.3                       # seconds between each dialog deleted in a wipe, so a big chat list doesn't trip a flood limit
BULK_KEY = "*"                       # Store.log() key for an action that isn't about one account (a global sweep)

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


# International calling code -> ISO-2, longest prefix first; common countries only, 🌐 for anything else.
_CALLING_CODES = {
    "1242": "bs", "1246": "bb", "1264": "ai", "1268": "ag", "1284": "vg", "1340": "vi", "1345": "ky", "1441": "bm",
    "1473": "gd", "1649": "tc", "1664": "ms", "1670": "mp", "1671": "gu", "1684": "as", "1758": "lc", "1767": "dm",
    "1784": "vc", "1787": "pr", "1809": "do", "1868": "tt", "1869": "kn", "1876": "jm", "1939": "pr",
    "1": "us", "20": "eg", "211": "ss", "212": "ma", "213": "dz", "216": "tn", "218": "ly", "220": "gm", "221": "sn",
    "222": "mr", "223": "ml", "224": "gn", "225": "ci", "226": "bf", "227": "ne", "228": "tg", "229": "bj",
    "230": "mu", "231": "lr", "232": "sl", "233": "gh", "234": "ng", "235": "td", "236": "cf", "237": "cm",
    "238": "cv", "239": "st", "240": "gq", "241": "ga", "242": "cg", "243": "cd", "244": "ao", "245": "gw",
    "248": "sc", "249": "sd", "250": "rw", "251": "et", "252": "so", "253": "dj", "254": "ke", "255": "tz",
    "256": "ug", "257": "bi", "258": "mz", "260": "zm", "261": "mg", "262": "re", "263": "zw", "264": "na",
    "265": "mw", "266": "ls", "267": "bw", "268": "sz", "269": "km", "27": "za", "30": "gr", "31": "nl",
    "32": "be", "33": "fr", "34": "es", "36": "hu", "39": "it", "40": "ro", "41": "ch", "420": "cz",
    "421": "sk", "423": "li", "43": "at", "44": "gb", "45": "dk", "46": "se", "47": "no", "48": "pl",
    "49": "de", "351": "pt", "352": "lu", "353": "ie", "354": "is", "355": "al", "356": "mt", "357": "cy",
    "358": "fi", "359": "bg", "370": "lt", "371": "lv", "372": "ee", "373": "md", "374": "am", "375": "by",
    "376": "ad", "377": "mc", "378": "sm", "380": "ua", "381": "rs", "382": "me", "383": "xk", "385": "hr",
    "386": "si", "387": "ba", "389": "mk", "51": "pe", "52": "mx", "53": "cu", "54": "ar", "55": "br",
    "56": "cl", "57": "co", "58": "ve", "590": "gp", "591": "bo", "592": "gy", "593": "ec", "595": "py",
    "598": "uy", "60": "my", "61": "au", "62": "id", "63": "ph", "64": "nz", "65": "sg", "66": "th",
    "7": "ru", "81": "jp", "82": "kr", "84": "vn", "852": "hk", "853": "mo", "855": "kh", "856": "la",
    "86": "cn", "880": "bd", "886": "tw", "90": "tr", "91": "in", "92": "pk", "93": "af", "94": "lk",
    "95": "mm", "960": "mv", "961": "lb", "962": "jo", "963": "sy", "964": "iq", "965": "kw", "966": "sa",
    "967": "ye", "968": "om", "970": "ps", "971": "ae", "972": "il", "973": "bh", "974": "qa", "975": "bt",
    "976": "mn", "977": "np", "98": "ir",
}


def guess_cc(phone: str) -> str:
    """ISO-2 country code guessed from a phone's leading digits (longest match wins); '' if nothing matches."""
    digits = re.sub(r"\D", "", phone)
    for n in (4, 3, 2, 1):
        if digits[:n] in _CALLING_CODES:
            return _CALLING_CODES[digits[:n]]
    return ""


def flag(cc: str) -> str:
    cc = cc.lower()
    return chr(0x1F1E6 + ord(cc[0]) - 97) + chr(0x1F1E6 + ord(cc[1]) - 97) if re.fullmatch(r"[a-z]{2}", cc) else "🌐"


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

    def save(self, throttle: float = 0.0) -> None:
        now = time.time()
        if throttle and now - getattr(self, "_saved", 0.0) < throttle:
            return
        self._saved = now
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def log(self, key: str, kind: str, text: str) -> None:
        """Persisted activity record (never put a code or any other secret in text). The admin is always the actor - single-admin bot, no attribution needed."""
        ev = self.d.setdefault("events", [])
        ev.append({"t": int(time.time()), "key": key, "k": kind, "x": str(text)[:200]})
        del ev[:-500]
        self.save(5.0)


# ── the manager: one TelegramClient per account that is being watched, short-lived ones otherwise ───────
class Manager:
    def __init__(self, store: Store, bot=None):
        self.store, self.bot = store, bot
        self.live: dict[str, Any] = {}             # phone -> connected TelegramClient (kept open while OTP-watch is on)
        self.watch_tasks: dict[str, asyncio.Task] = {}
        self.login: dict | None = None             # the one in-progress phone+code login (single admin)
        self.login_timer: asyncio.TimerHandle | None = None
        self.clone: dict | None = None             # the one in-progress clone (single admin): read the code from the SOURCE account's own session
        self.clone_timer: asyncio.TimerHandle | None = None
        self.health_task: asyncio.Task | None = None
        self.bulk_running: set[str] = set()        # which admin-triggered sweep ("val" / "kill") is in flight, to refuse a double-tap
        self.wiping: set[str] = set()              # phones currently mid-wipe, to refuse a double-tap on the same account
        os.makedirs(SESS_DIR, exist_ok=True)

    def base(self, phone: str) -> str:
        return os.path.join(SESS_DIR, re.sub(r"[^0-9A-Za-z+_#-]", "", phone) or phone)      # keep letters/# too: the id{n} fallback and #2/#3 clone slots must not collide on disk

    def allowed(self, uid: int) -> bool:
        return self.store.d["admin"] == uid

    # -- connections
    async def open(self, phone: str):
        """Connect to a session file, retrying past a transient sqlite lock (e.g. a stale -journal left by an
        earlier run that didn't shut down cleanly - a GitHub Actions cache restore can also land one mid-write)."""
        from telethon import TelegramClient
        cr = creds()
        if not cr:
            raise RuntimeError("No TG_API_ID / TG_API_HASH configured.")
        base = self.base(phone)
        last: Exception | None = None
        for attempt in range(3):
            try:
                c = TelegramClient(base, cr[0], cr[1])
                await c.connect()
                return c
            except Exception as e:                                       # noqa: BLE001  (sqlite3.OperationalError and friends - never imported directly)
                last = e
                if attempt == 1:                                          # one cleanup pass, then one more try
                    for ext in ("-journal", "-wal", "-shm"):
                        try:
                            os.remove(base + ".session" + ext)
                        except FileNotFoundError:
                            pass
                await asyncio.sleep(0.3)
        raise last

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
                self.store.log(phone, "code", "relayed")                 # never the text itself: it's the login code
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
        await self._cleanup_clone()
        if self.health_task and not self.health_task.done():
            self.health_task.cancel()
            try:
                await self.health_task
            except (asyncio.CancelledError, Exception):                 # noqa: BLE001
                pass

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
        acc = self.store.d["accounts"].setdefault(phone, {})
        acc["added_at"], acc["name"], acc["phone"] = acc.get("added_at") or time.time(), who.strip() or phone, phone
        acc["health"], acc["checked_at"] = "ok", time.time()           # it was just proven authorized above
        self.store.save()
        self.store.log(phone, "import", "updated" if existed else "added")
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
    async def check_health(self, phone: str) -> tuple[str, str]:
        """-> (code, detail). code: 'ok' | 'invalid' | 'banned' | 'connect_error' | 'error'. Used by the Validate button and the background sweep."""
        from telethon.errors import AuthKeyUnregisteredError, UserDeactivatedBanError, UserDeactivatedError
        try:
            c, mine = await self.client_for(phone)
        except Exception as e:                                           # noqa: BLE001
            return "connect_error", type(e).__name__
        try:
            if not await c.is_user_authorized():
                return "invalid", ""
            me = await c.get_me()
            who = (me.first_name or "") + (f" (@{me.username})" if me.username else "")
            return "ok", who.strip()
        except AuthKeyUnregisteredError:
            return "invalid", ""
        except (UserDeactivatedError, UserDeactivatedBanError):
            return "banned", ""
        except Exception as e:                                           # noqa: BLE001
            return "error", type(e).__name__
        finally:
            if mine:
                await c.disconnect()

    def record_health(self, phone: str, code: str) -> bool:
        """Persists the latest health reading and logs a real transition. -> True only for a NEW bad transition (worth alerting on)."""
        acc = self.store.d["accounts"].get(phone)
        if acc is None:
            return False
        was_bad, is_bad = acc.get("health") in ("invalid", "banned"), code in ("invalid", "banned")
        acc["health"], acc["checked_at"] = code, time.time()
        self.store.save(5.0)
        if is_bad and not was_bad:
            self.store.log(phone, "health", f"went {code}")
            return True
        if code == "ok" and was_bad:
            self.store.log(phone, "health", "recovered")
        return False

    async def validate(self, phone: str) -> str:
        code, info = await self.check_health(phone)
        self.record_health(phone, code)
        return {"ok": f"✅ Still logged in as <b>{html.escape(info)}</b>.", "invalid": "❌ Not logged in any more.",
                "banned": "❌ This account is deactivated or banned.", "connect_error": f"❌ Couldn't connect ({html.escape(info)}).",
                "error": f"❌ Couldn't check it: {html.escape(info)}."}[code]

    async def health_loop(self) -> None:
        """Every HEALTH_INTERVAL, re-checks every account (gaps between accounts so a big pool doesn't open 100 connections at once);
        DMs the admin the moment one newly goes invalid/banned instead of them finding out next time they tap it."""
        while True:
            try:
                await asyncio.sleep(HEALTH_INTERVAL)
            except asyncio.CancelledError:
                raise
            for phone in list(self.store.d["accounts"]):
                if phone not in self.store.d["accounts"]:                # removed mid-sweep
                    continue
                try:
                    code, _info = await self.check_health(phone)
                except Exception:                                        # noqa: BLE001
                    code = "error"
                if self.record_health(phone, code):
                    name = self.store.d["accounts"][phone].get("name") or phone
                    await self.notify(f"🩺 <b>{html.escape(name)}</b> ({html.escape(phone)}) is now <b>{code}</b> - its Telegram session stopped working.")
                await asyncio.sleep(HEALTH_GAP)

    def start_health(self) -> None:
        if self.health_task is None or self.health_task.done():
            self.health_task = asyncio.create_task(self.health_loop())

    async def validate_all(self) -> None:
        """Admin-triggered sweep of every account right now (⏳ immediate reply, full summary DMed when done)."""
        if "val" in self.bulk_running:
            return
        self.bulk_running.add("val")
        try:
            accs = list(self.store.d["accounts"])
            ok = bad = err = 0
            newly_bad = []
            for phone in accs:
                if phone not in self.store.d["accounts"]:               # removed mid-sweep
                    continue
                try:
                    code, _info = await self.check_health(phone)
                except Exception:                                        # noqa: BLE001
                    code = "error"
                if self.record_health(phone, code):
                    newly_bad.append(phone)
                if code == "ok":
                    ok += 1
                elif code in ("invalid", "banned"):
                    bad += 1
                else:
                    err += 1
                await asyncio.sleep(HEALTH_GAP)
            self.store.d["last_validate_all"] = time.time()
            self.store.save()
            self.store.log(BULK_KEY, "validate_all", f"{ok} ok, {bad} bad, {err} couldn't check ({len(accs)} total)")
            lines = [f"✅ <b>Validate all</b> done: {ok} ok, {bad} bad, {err} couldn't check ({len(accs)} total)."]
            if newly_bad:
                names = [html.escape((self.store.d['accounts'].get(p) or {}).get('name') or p) for p in newly_bad]
                lines.append("⚠️ Newly bad: " + ", ".join(names))
            await self.notify("\n".join(lines))
        finally:
            self.bulk_running.discard("val")

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
            self.store.log(phone, "kill", "ended one other device")
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
            self.store.log(phone, "kill_all", "ended every other device")
            return "✅ Every other device is logged out. Only this bot's session remains."
        except FloodWaitError as e:
            return f"⏳ Telegram limits this to about once a day - wait {e.seconds}s and try again."
        except Exception as e:                                           # noqa: BLE001
            return f"❌ Couldn't do it: {html.escape(type(e).__name__)}."
        finally:
            if mine:
                await c.disconnect()

    async def kill_all_others_everywhere(self) -> None:
        """Admin-triggered: end every OTHER device on every account right now. Each account's own bot session stays untouched."""
        if "kill" in self.bulk_running:
            return
        self.bulk_running.add("kill")
        try:
            accs = list(self.store.d["accounts"])
            done = flood = failed = 0
            notes = []
            for phone in accs:
                if phone not in self.store.d["accounts"]:               # removed mid-sweep
                    continue
                msg = await self.end_all_others(phone)
                name = (self.store.d["accounts"][phone].get("name") or phone)
                if msg.startswith("✅"):
                    done += 1
                elif msg.startswith("⏳"):
                    flood += 1
                    notes.append(f"⏳ {html.escape(name)}: rate-limited (~once/day)")
                else:
                    failed += 1
                    notes.append(f"❌ {html.escape(name)}: {msg}")
                await asyncio.sleep(HEALTH_GAP)
            self.store.d["last_kill_all"] = time.time()
            self.store.save()
            self.store.log(BULK_KEY, "kill_all_everywhere", f"{done} cleared, {flood} rate-limited, {failed} failed ({len(accs)} total)")
            lines = [f"🧹 <b>Kill others (all accounts)</b> done: {done} cleared, {flood} rate-limited, {failed} failed ({len(accs)} total)."]
            lines.extend(notes[:15])
            await self.notify("\n".join(lines))
        finally:
            self.bulk_running.discard("kill")

    async def wipe_account(self, phone: str) -> tuple[int, int, list[str]]:
        """Deletes every chat (both sides), leaves every group/channel/bot, and removes every contact. Irreversible;
        the account itself stays logged in - only its chats/contacts/memberships go. -> (dialogs_cleared, contacts_removed, errors)."""
        from telethon.tl.functions.contacts import DeleteContactsRequest, GetContactsRequest
        try:
            c, mine = await self.client_for(phone)
        except Exception as e:                                           # noqa: BLE001
            return 0, 0, [f"couldn't connect: {type(e).__name__}"]
        cleared = 0
        errors: list[str] = []
        try:
            async for d in c.iter_dialogs():
                try:
                    await c.delete_dialog(d.entity, revoke=True)
                    cleared += 1
                except Exception as e:                                   # noqa: BLE001
                    errors.append(f"{html.escape((d.name or str(d.id))[:30])}: {type(e).__name__}")
                await asyncio.sleep(WIPE_GAP)
            removed = 0
            try:
                res = await c(GetContactsRequest(hash=0))
                users = getattr(res, "users", [])
                if users:
                    await c(DeleteContactsRequest(id=users))
                    removed = len(users)
            except Exception as e:                                       # noqa: BLE001
                errors.append(f"contacts: {type(e).__name__}")
        finally:
            if mine:
                await c.disconnect()
        return cleared, removed, errors

    async def wipe_account_task(self, phone: str) -> None:
        """Admin-triggered: run wipe_account() in the background and DM the result (⏳ immediate reply comes from the caller)."""
        if phone in self.wiping:
            return
        self.wiping.add(phone)
        try:
            cleared, removed, errors = await self.wipe_account(phone)
            name = (self.store.d["accounts"].get(phone) or {}).get("name") or phone
            self.store.log(phone, "wipe", f"{cleared} dialogs cleared, {removed} contacts removed, {len(errors)} errors")
            lines = [f"💣 <b>Wipe done</b> for <b>{html.escape(name)}</b>: {cleared} chat/group/channel/bot dialog(s) cleared, "
                     f"{removed} contact(s) removed."]
            if errors:
                lines.append(f"⚠️ {len(errors)} item(s) failed: " + ", ".join(errors[:10]))
            await self.notify("\n".join(lines))
        finally:
            self.wiping.discard(phone)

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
        self.store.log(phone, "logout", "logged out everywhere")
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

    # -- clone: a brand-new, separate authorized session for an account that already has one, with the code
    # read automatically off the EXISTING session's own incoming messages - nobody ever types or sees the code.
    def arm_clone(self) -> None:
        if self.clone_timer:
            self.clone_timer.cancel()
        self.clone_timer = asyncio.get_running_loop().call_later(CLONE_TTL, lambda: asyncio.create_task(self.clone_expire()))

    async def clone_expire(self) -> None:
        await self.notify("⏱ The clone timed out. Send /menu → account → 🧬 Clone session to try again.")
        await self._cleanup_clone()

    async def start_clone(self, source_key: str) -> tuple[bool, str]:
        """-> (True, new account key) once the code-wait is underway, or (False, reason)."""
        acc = self.store.d["accounts"].get(source_key)
        if acc is None:
            return False, "Unknown account."
        phone = acc.get("phone") or source_key
        try:
            src, src_mine = await self.client_for(source_key)
        except Exception as e:                                           # noqa: BLE001
            return False, f"Couldn't connect to the existing session: {html.escape(type(e).__name__)}."
        try:
            if not await src.is_user_authorized():
                if src_mine:
                    await src.disconnect()
                return False, "The existing session isn't logged in any more - nothing to clone from."
        except Exception as e:                                           # noqa: BLE001
            if src_mine:
                await src.disconnect()
            return False, f"Couldn't check the existing session: {html.escape(type(e).__name__)}."
        n = 2
        while f"{phone}#{n}" in self.store.d["accounts"]:
            n += 1
        new_key = f"{phone}#{n}"
        try:
            new_c = await self.open(new_key)
        except Exception as e:                                           # noqa: BLE001
            if src_mine:
                await src.disconnect()
            return False, f"Couldn't start the new session: {html.escape(type(e).__name__)}."
        try:
            sent = await new_c.send_code_request(phone)
        except Exception as e:                                           # noqa: BLE001
            await new_c.disconnect()
            if src_mine:
                await src.disconnect()
            return False, f"Couldn't request a code: {html.escape(type(e).__name__)}."
        from telethon import events
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        async def _catch(ev):                                            # noqa: ANN001
            m = re.search(r"\b(\d{5,6})\b", ev.raw_text or "")
            if m and not fut.done():
                fut.set_result(m.group(1))
        src.add_event_handler(_catch, events.NewMessage(chats=TG_SERVICE_CHAT))
        self.clone = {"source_key": source_key, "source_client": src, "source_mine": src_mine, "phone": phone,
                      "new_key": new_key, "new_client": new_c, "hash": sent.phone_code_hash, "future": fut,
                      "handler": _catch, "stage": "code"}
        self.arm_clone()
        self.store.log(source_key, "clone", f"started -> {new_key}")
        return True, new_key

    async def run_clone_wait(self) -> None:
        """Waits (bounded) for the auto-caught code, signs the new session in, and reports the outcome - never blocks the bot."""
        from telethon.errors import SessionPasswordNeededError
        clone = self.clone
        if not clone:
            return
        try:
            code = await asyncio.wait_for(clone["future"], CLONE_WAIT)
        except asyncio.TimeoutError:
            self.store.log(clone["source_key"], "clone", f"{clone['new_key']} timed out")
            await self.notify(f"⌛ Clone of <code>{html.escape(clone['phone'])}</code> timed out - no code arrived within {int(CLONE_WAIT)}s.")
            await self._cleanup_clone()
            return
        try:
            await clone["new_client"].sign_in(phone=clone["phone"], code=code, phone_code_hash=clone["hash"])
        except SessionPasswordNeededError:
            clone["stage"] = "pw"
            self.arm_clone()
            await self.notify(f"🔒 Clone of <code>{html.escape(clone['phone'])}</code>: this account has 2-step verification. "
                              "Send its password in this chat. I delete your message right after.")
            return
        except Exception as e:                                           # noqa: BLE001
            self.store.log(clone["source_key"], "clone", f"{clone['new_key']} failed: {type(e).__name__}")
            await self.notify(f"❌ Clone of <code>{html.escape(clone['phone'])}</code> failed: {html.escape(type(e).__name__)}.")
            await self._cleanup_clone()
            return
        await self._finish_clone_success()

    async def clone_sign_in_password(self, password: str) -> tuple[bool, str]:
        """-> (done, message). done=False: wrong password, still waiting. done=True: resolved (success or given up)."""
        from telethon.errors import PasswordHashInvalidError
        clone = self.clone
        if not clone or clone.get("stage") != "pw":
            return True, "No clone is waiting for a password."
        self.arm_clone()
        try:
            await clone["new_client"].sign_in(password=password)
        except PasswordHashInvalidError:
            return False, "❌ Wrong password. Send it again, or Cancel."
        except Exception as e:                                           # noqa: BLE001
            await self._cleanup_clone()
            return True, f"❌ Clone failed: {html.escape(type(e).__name__)}."
        await self._finish_clone_success()
        return True, "✅ Done - check the notification above."

    async def _finish_clone_success(self) -> None:
        clone = self.clone
        me = await clone["new_client"].get_me()
        who = (me.first_name or "") + (f" (@{me.username})" if me.username else "")
        try:
            clone["new_client"].session.save()                           # persists to disk independent of the connection - safe to disconnect right after
        except Exception:                                                # noqa: BLE001
            pass
        await self._teardown_clone()
        self.store.d["accounts"][clone["new_key"]] = {"watch": False, "added_at": time.time(), "name": who.strip() or clone["phone"],
                                                        "phone": clone["phone"], "clone_of": clone["source_key"], "health": "ok", "checked_at": time.time()}
        self.store.save()
        self.store.log(clone["source_key"], "clone", f"{clone['new_key']} ready")
        await self.notify(f"🧬 Clone ready: <b>{html.escape(who.strip())}</b> ({html.escape(clone['phone'])}) is now its own, separate session "
                          f"(<code>{html.escape(clone['new_key'])}</code>), alongside the one it was cloned from.")

    async def _cleanup_clone(self) -> None:
        if not self.clone:
            return
        new_key = self.clone["new_key"]
        await self._teardown_clone()
        for ext in (".session", ".session-journal"):                     # the sign-in never completed: nothing to keep
            try:
                os.remove(self.base(new_key) + ext)
            except FileNotFoundError:
                pass

    async def _teardown_clone(self) -> None:
        """Close both clients and clear self.clone. Whether the new session's file is kept or deleted is the caller's decision, made after this returns."""
        clone, self.clone = self.clone, None
        if not clone:
            return
        from telethon import events
        try:
            clone["source_client"].remove_event_handler(clone["handler"], events.NewMessage)
        except Exception:                                                # noqa: BLE001
            pass
        if clone.get("source_mine"):
            try:
                await clone["source_client"].disconnect()
            except Exception:                                            # noqa: BLE001
                pass
        try:
            await clone["new_client"].disconnect()
        except Exception:                                                # noqa: BLE001
            pass
        if self.clone_timer:
            self.clone_timer.cancel()
            self.clone_timer = None


# ── the bot ───────────────────────────────────────────────────────────────────────────────────────────
def _nav() -> KB:
    return [[("🏠 Menu", "m")]]


def health_dot(acc: dict) -> str:
    """🟢 healthy and checked recently · 🔴 invalid/banned · 🟡 unknown, never checked, or stale."""
    h = acc.get("health")
    if h in ("invalid", "banned"):
        return "🔴"
    if h == "ok" and time.time() - (acc.get("checked_at") or 0) < HEALTH_STALE:
        return "🟢"
    return "🟡"


def health_word(acc: dict) -> str:
    h = acc.get("health")
    if h in ("invalid", "banned"):
        return f"🔴 {h}"
    if h == "ok":
        return f"🟢 ok (checked {ago(acc['checked_at'])})" if acc.get("checked_at") else "🟡 not checked yet"
    return "🟡 not checked yet" if not acc.get("checked_at") else f"🟡 unclear (checked {ago(acc['checked_at'])})"


def main_screen(mgr: Manager) -> Reply:
    d = mgr.store.d
    accs = d["accounts"]
    groups = account_groups(mgr)
    bad = sum(1 for a in accs.values() if a.get("health") in ("invalid", "banned"))
    day_ago = time.time() - 86400
    codes_today = sum(1 for e in d.get("events", []) if e["k"] == "code" and e["t"] > day_ago)
    txt = (f"🔐 <b>Session manager</b>\n{len(groups)} account{'s' if len(groups) != 1 else ''} · {len(accs)} session{'s' if len(accs) != 1 else ''} total · "
           f"{sum(1 for a in accs.values() if a.get('watch'))} watched\n"
           + (f"⚠️ {bad} need attention\n" if bad else "")
           + f"📩 {codes_today} code{'s' if codes_today != 1 else ''} today"
           + (f"\n🔁 Last validate all: {ago(d['last_validate_all'])}" if d.get("last_validate_all") else "")
           + (f"\n🧹 Last kill-all sweep: {ago(d['last_kill_all'])}" if d.get("last_kill_all") else ""))
    kb: KB = [[("📂 Accounts", "a:l")], [("➕ Add account", "a:add"), ("🧾 Activity", "ev:l")],
              [("✅ Validate all", "ga:val"), ("🧹 Kill others (all)", "ga:kill")]]
    return txt, kb


def group_of(key: str, acc: dict) -> str:
    """The real phone number an account key belongs to (a clone's own key is '<phone>#2', '#3', ...)."""
    return acc.get("phone") or key.split("#")[0]


def account_groups(mgr: Manager) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for key, acc in mgr.store.d["accounts"].items():
        groups.setdefault(group_of(key, acc), []).append(key)
    for keys in groups.values():
        keys.sort(key=lambda k: mgr.store.d["accounts"][k].get("added_at") or 0)   # the original session first, then its clones in order
    return groups


def group_dot(mgr: Manager, keys: list[str]) -> str:
    """The worst status among a group's sessions - one bad clone is enough to flag the whole row."""
    dots = [health_dot(mgr.store.d["accounts"][k]) for k in keys]
    return "🔴" if "🔴" in dots else "🟡" if "🟡" in dots else "🟢"


def accounts_screen(mgr: Manager) -> Reply:
    accs = mgr.store.d["accounts"]
    if not accs:
        return "📂 <b>Accounts</b>\nNone yet. Add one below.", [[("➕ Add account", "a:add")], [("🏠 Menu", "m")]]
    groups = account_groups(mgr)
    kb: KB = []
    for phone, keys in sorted(groups.items(), key=lambda kv: -max(accs[k].get("added_at") or 0 for k in kv[1])):
        primary = accs[keys[0]]
        name = primary.get("name") or phone
        cc = guess_cc(phone)
        label = f"{group_dot(mgr, keys)}{flag(cc)}" + (f" {cc.upper()}" if cc else "") + f" · {name[:16]} · {phone} · {len(keys)} 🔐"
        kb.append([(label[:64], f"a:v:{keys[0]}" if len(keys) == 1 else f"a:g:{phone}")])
    kb.append([("✅ Validate all", "ga:val"), ("🧹 Kill others (all)", "ga:kill")])
    kb.append([("➕ Add account", "a:add")])
    kb.append([("🏠 Menu", "m")])
    return f"📂 <b>Accounts</b> ({len(groups)})\n<i>🟢 healthy · 🟡 check pending · 🔴 needs attention - the number = sessions on that account</i>", kb


def group_screen(mgr: Manager, phone: str) -> Reply:
    accs = mgr.store.d["accounts"]
    keys = account_groups(mgr).get(phone, [])
    if not keys:
        return "I don't manage that account any more.", [[("⬅️ Accounts", "a:l")]]
    if len(keys) == 1:
        return account_card(mgr, keys[0])
    name = accs[keys[0]].get("name") or phone
    kb: KB = []
    for i, k in enumerate(keys):
        a = accs[k]
        tag = "Original" if not a.get("clone_of") else f"Clone #{i + 1}"
        watch = "📡" if a.get("watch") else "⚪"
        kb.append([(f"{health_dot(a)}{watch} {tag} · added {ago(a.get('added_at') or time.time())}", f"a:v:{k}")])
    kb.append([("⬅️ Accounts", "a:l"), ("🏠 Menu", "m")])
    cc = guess_cc(phone)
    return (f"{flag(cc)}" + (f" {cc.upper()}" if cc else "") + f" · <b>{html.escape(name)}</b> · <code>{html.escape(phone)}</code>\n"
            f"{len(keys)} sessions on this account - pick one to work on:"), kb


def account_card(mgr: Manager, phone: str, note: str = "") -> Reply:
    acc = mgr.store.d["accounts"].get(phone)
    if acc is None:
        return "I don't manage that account any more.", [[("⬅️ Accounts", "a:l")]]
    watch = bool(acc.get("watch"))
    src = acc.get("clone_of")
    src_name = (mgr.store.d["accounts"].get(src) or {}).get("name") if src else None
    txt = (f"👤 <b>{html.escape(acc.get('name') or phone)}</b>\n📞 <code>{html.escape(acc.get('phone') or phone)}</code>\n"
           + (f"🧬 Clone of {html.escape(src_name or src)}\n" if src else "")
           + f"Added {ago(acc.get('added_at') or time.time())}\n🩺 Health: {health_word(acc)}\n📡 OTP watch: {'ON' if watch else 'off'}"
           + (f"\n\n{note}" if note else ""))
    group = group_of(phone, acc)
    back = ("⬅️ Sessions on this account", f"a:g:{group}") if len(account_groups(mgr).get(group, [])) > 1 else ("⬅️ Accounts", "a:l")
    kb: KB = [[("🔁 Validate", f"a:val:{phone}"), ("📟 Sessions", f"a:s:{phone}:0")],
              [(("📡 Watch: ON" if watch else "📡 Watch: off"), f"a:w:{phone}")],
              [("🧬 Clone session", f"cl:start:{phone}")],
              [("🧾 Activity", f"ev:l:{phone}:0")],
              [("📤 Export session", f"a:exp:{phone}"), ("🗑 Remove", f"a:rm:{phone}")],
              [("🚪 Logout everywhere", f"a:lo:{phone}")],
              [("💣 Wipe chats/contacts", f"a:wipe:{phone}")],
              [back, ("🏠 Menu", "m")]]
    return txt, kb


def events_screen(mgr: Manager, page: int, filter_key: str | None) -> Reply:
    evs = [e for e in reversed(mgr.store.d.get("events", [])) if filter_key is None or e["key"] == filter_key]
    per = 12
    pages = max(1, (len(evs) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    part = evs[page * per:(page + 1) * per]
    base = "ev:l" if filter_key is None else f"ev:l:{filter_key}"
    rows = [f"{EVENT_ICON.get(e['k'], '•')} <b>{ago(e['t'])}</b>" + ("" if filter_key else f" · <code>{html.escape(e['key'])}</code>") + f"\n    {html.escape(e['x'])}"
            for e in part]
    nav = []
    if page > 0:
        nav.append(("⬅️", f"{base}:{page - 1}"))
    nav.append((f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("➡️", f"{base}:{page + 1}"))
    back = ("⬅️ Account", f"a:v:{filter_key}") if filter_key else ("🏠 Menu", "m")
    kb: KB = [nav, [("🔄 Refresh", f"{base}:{page}"), back]]
    who = f" for <code>{html.escape(filter_key)}</code>" if filter_key else ""
    return f"🧾 <b>Activity</b>{who} ({len(evs)}, newest first)\n" + ("\n".join(rows) or "nothing yet"), kb


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
    cur = next((a for a in auths if getattr(a, "current", False)), None)
    head = (f"📍 <b>This bot's own session</b> was created {ago(cur.date_created)}, last active {ago(cur.date_active)}.\n" if cur and getattr(cur, "date_created", None) else "")
    lines = [f"📱 {a.device_model or a.platform or '?'} ({a.app_name or '?'})" + (" · 📍 this bot" if getattr(a, "current", False) else "")
             + f"\n   {a.country or '?'} · created {ago(a.date_created) if getattr(a, 'date_created', None) else '?'}"
             f" · last active {ago(a.date_active) if getattr(a, 'date_active', None) else '?'}" for a in auths]
    return head + f"📟 <b>Active sessions</b> ({len(auths)})\n" + pre(lines), kb


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
        clone = self.mgr.clone
        if clone and clone.get("stage") == "pw" and not cmd.startswith("/"):
            done, msg = await self.mgr.clone_sign_in_password(text)
            return msg, (_nav() if done else [[("Cancel", "cl:x")]])
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
        if k == "cl":
            return await self.ui_clone(p)
        if k == "ga":
            return await self.ui_global(p)
        if k == "ev":
            sub = p[1] if len(p) > 1 else "l"
            if sub == "l" and len(p) == 2:                               # ev:l -> global activity, page 0
                return events_screen(self.mgr, 0, None)
            if sub == "l" and len(p) == 3:                               # ev:l:<page>
                return events_screen(self.mgr, int(p[2]), None)
            if sub == "l" and len(p) == 4:                               # ev:l:<key>:<page> -> that account's own activity
                return events_screen(self.mgr, int(p[3]), p[2])
            return events_screen(self.mgr, 0, None)
        return main_screen(self.mgr)

    async def ui_clone(self, p: list[str]) -> Reply | None:
        sub = p[1] if len(p) > 1 else ""
        if sub == "x":
            await self.mgr._cleanup_clone()
            return "Cancelled.", _nav()
        if sub == "start":
            key = p[2]
            if self.mgr.clone:
                return "A clone is already in progress. Cancel it first.", [[("Cancel", "cl:x")]]
            ok, info = await self.mgr.start_clone(key)
            if not ok:
                return account_card(self.mgr, key, f"❌ {info}")
            asyncio.create_task(self.mgr.run_clone_wait())
            name = (self.mgr.store.d["accounts"].get(key) or {}).get("name") or key
            return (f"🧬 Cloning <b>{html.escape(name)}</b>…\n📡 Waiting for the code - read automatically from its existing session, "
                    f"nothing to type or see. This can take up to {int(CLONE_WAIT)}s; I'll message you the result."), [[("Cancel", "cl:x")]]
        return main_screen(self.mgr)

    async def ui_global(self, p: list[str]) -> Reply | None:
        sub = p[1] if len(p) > 1 else ""
        n = len(self.mgr.store.d["accounts"])
        if sub == "val":
            if n == 0:
                return "No accounts to validate.", _nav()
            if "val" in self.mgr.bulk_running:
                return "A validate-all sweep is already running.", _nav()
            asyncio.create_task(self.mgr.validate_all())
            return f"⏳ Validating all {n} account{'s' if n != 1 else ''} in the background. I'll DM a summary when it's done.", _nav()
        if sub == "kill":
            if n == 0:
                return "No accounts to act on.", _nav()
            return (f"🧹 Log out every <b>OTHER</b> device on all {n} account{'s' if n != 1 else ''}? Each account's own bot session stays. "
                    "Telegram limits this to about once a day per account - any account hit recently will just report a wait."), \
                   [[("✅ Yes, kill all", "ga:killy"), ("Cancel", "m")]]
        if sub == "killy":
            if n == 0:
                return "No accounts to act on.", _nav()
            if "kill" in self.mgr.bulk_running:
                return "A kill-all sweep is already running.", _nav()
            asyncio.create_task(self.mgr.kill_all_others_everywhere())
            return f"⏳ Killing other sessions on all {n} account{'s' if n != 1 else ''} in the background. I'll DM a summary when it's done.", _nav()
        return main_screen(self.mgr)

    async def ui_account(self, p: list[str]) -> Reply | None:
        sub = p[1]
        if sub == "l":
            return accounts_screen(self.mgr)
        if sub == "g":
            return group_screen(self.mgr, p[2])
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
            self.mgr.store.log(phone, "watch", "on" if acc["watch"] else "off")
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
            self.mgr.store.log(p[2], "remove", "removed from the bot (Telegram login untouched)")
            self.mgr.remove(p[2])
            return "🗑 Removed.", accounts_screen(self.mgr)[1]
        if sub == "lo":
            name = (self.mgr.store.d["accounts"].get(p[2]) or {}).get("name") or p[2]
            return f"🚪 Log <b>{html.escape(name)}</b> out everywhere? This ends every device, including this bot's own session.", \
                   [[("✅ Yes, log out", f"a:loy:{p[2]}"), ("Cancel", f"a:v:{p[2]}")]]
        if sub == "loy":
            msg = await self.mgr.logout(p[2])
            return msg, accounts_screen(self.mgr)[1]
        if sub == "wipe":
            phone = p[2]
            if phone not in self.mgr.store.d["accounts"]:
                return "I don't manage that account any more.", [[("⬅️ Accounts", "a:l")]]
            name = (self.mgr.store.d["accounts"].get(phone) or {}).get("name") or phone
            return (f"💣 <b>Wipe {html.escape(name)}</b>?\nThis <b>permanently</b> deletes every chat (for both sides), leaves every "
                    "group/channel/bot, and removes every contact. It cannot be undone. The account itself stays logged in - "
                    "only its chats/contacts/memberships go."), \
                   [[("✅ Yes, wipe everything", f"a:wipey:{phone}"), ("Cancel", f"a:v:{phone}")]]
        if sub == "wipey":
            phone = p[2]
            acc = self.mgr.store.d["accounts"].get(phone)
            if acc is None:
                return "I don't manage that account any more.", [[("⬅️ Accounts", "a:l")]]
            if phone in self.mgr.wiping:
                return "Already wiping that account. I'll DM you when it's done.", _nav()
            asyncio.create_task(self.mgr.wipe_account_task(phone))
            return f"⏳ Wiping everything on <b>{html.escape(acc.get('name') or phone)}</b> in the background. I'll DM a summary when it's done.", _nav()
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
        was_clone_pw = bool(mgr.clone and mgr.clone.get("stage") == "pw")   # capture before handle() can resolve (and clear) it
        res = await sb.handle(m.from_user.id, m.text)
        if mgr.login or was_clone_pw:
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
    mgr.start_health()
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
