"""SmsCenter price bot - ALL platforms, controlled from Telegram.

HUNTER (buying): 🏹 Hunter -> make a hunter: platform, country, operator, number PREFIX, max price, how many numbers you want (counted
after the prefix filter) and HOW OFTEN it sends a request (0.1s, 1s ... you type it). It keeps asking SmsCenter for a number; a number that
doesn't match the prefix (or is above your max price) is released at once; a matching one is kept, you get "✅ Bought successfully" with a
🔓 Release button (cancel + balance back), and when the code arrives you get a NEW message with the code and its number. The ⏸ Pause button
only stops the price monitoring - never a hunter. 📜 History lists what you bought.

AUTO-CHECK: every number a hunter buys is sent to the Telegram checker bot (@tgchecker3bababot) with YOUR Telethon session - any *.session file
in this scripts folder (api id/hash + bot name are read from the NumberHub app's saved settings, or SMSCENTER_TG_SESSION / _API_ID / _API_HASH / _CHECK_BOT).
Banned or already-registered numbers are cancelled at once (balance back) and the hunter buys another; a clean one is kept, the message is PINNED
(unpinned again if you cancel it) with 📋 copy buttons, and the OTP arrives as a reply under that number's message, showing its status.

PRICE WATCH: pick a platform and a PRICE. The bot looks at EVERY country for that platform and
  - the first time it sends you the list of all countries that are under your price;
  - after that it stays quiet unless something changes, and then it tells you exactly what:
      🆕 a country that just went under   ·   📉 a country whose price dropped (old → new)
      📈 a price that went up (still under)   ·   🚫 a country that is no longer under (or sold out)
  Each watch has its own price, price basis (API price = fast, or Cheapest tier) and check interval. Buttons: 🎯 Price watch.
  Commands: /watch [platform] <price>  ·  /watches  ·  /unwatch <id|all>

Watches the price of numbers on smscenter.pro for any platform (Telegram, WhatsApp, ...) in any country, and messages you
when something changes. Every (platform, country) pair is its own "track" with its OWN settings and its OWN check interval.

Run:   python scripts/smscenter_price_bot.py
  First time it asks for the bot token (from @BotFather). Open the bot and send /start: the first person to do that becomes
  the ADMIN. MANY USERS can use the bot: every user has their own profile (own SmsCenter key via  /setkey <key>, own tracks,
  own intervals and alerts) and alerts are sent to them privately. The admin can close the bot to everyone else:
  /access open|closed · /allow <id> · /deny <id> · /users  · /session (new checker Telethon session)

How you use it (all buttons):
  1. 📱 Platforms  -> pick a platform (e.g. Telegram)
  2. pick a country (e.g. Ukraine)  -> you see the live prices (all 3 endpoints) and the CONTROL CENTER
  3. ▶️ Start tracking, and choose how often to look: 1s / 5s / 10s / 30s / 60s or any number of seconds you type

The three endpoints (checked against the live API for Telegram; other platforms use the same calls with their service code):
  getOfferNumberList?country=..&services=<svc>  -> {"<svc>": {"minPrice", "apiPrice", "totalCount", "priceMap": {price: count}}}
  getCountNumbersList?country=..                -> {"<svc>": [{"name", "cost", "quantity"}]}   (a LIST, not an object as the docs say)
  getCountNumber?service=<svc>&country=..&price=true -> {"<svc>": <count>, "price": <price>}   ("badService" = not offered there)

Per-track alert settings (all optional, combine freely):
  any change     alert whenever the cheapest tier or the API price moves, up or down
  drop           alert when a price falls by at least an AMOUNT (0.05) or a PERCENT (10%)  - default is the global % drop
  under          alert once when a price reaches this amount or lower (re-arms when it goes back above)
  every          how often THIS track is checked, in seconds (default = global interval)
  mute           silence that track

Commands: /menu /platforms /countries [platform] /prices [platform] <country> /add [platform] <country> (start tracking)
          /stop [platform] <country> /remove [platform] <country>
          /set [platform] <country> any on|off · drop <0.05|10%|off|default> · under <price|off> · every <seconds|default>
          /list /trend [platform] <country> /interval <sec> /drop /mute /setkey /check /pause /resume /status /help
  "[platform]" is a code or name (tg, telegram, wa, whatsapp ...). Leave it out to use the last platform you opened.

The minimum check interval is 1 second (set SMSCENTER_MIN_INTERVAL to change it). Very short intervals on many tracks mean
many API calls per second - SmsCenter may rate-limit you.
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
import time
from typing import Any, Awaitable, Callable

import httpx

HERE_DIR = os.path.dirname(os.path.abspath(__file__))
if HERE_DIR not in sys.path:
    sys.path.insert(0, HERE_DIR)
import catchall  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _admin_id() -> int | None:
    """Admin Telegram id: SMSCENTER_ADMIN_ID if set and numeric, else admin_id from config.json next to the script."""
    env = os.environ.get("SMSCENTER_ADMIN_ID", "").strip()
    if env.isdigit():
        return int(env)
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            v = json.load(f).get("admin_id")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


STATE_PATH = os.environ.get("SMSCENTER_BOT_STATE") or os.path.join(HERE, ".smscenter_bot.json")
BASE_URL = "https://smscenter.pro/api"
DEFAULT_SVC = "tg"
MIN_INTERVAL = float(os.environ.get("SMSCENTER_MIN_INTERVAL") or 1)
PER_PAGE = 16
HIST_MAX = 40
ENDPOINTS = {"offer": "getOfferNumberList", "list": "getCountNumbersList", "count": "getCountNumber"}
INT_PRESETS = (1, 5, 10, 30, 60)                      # per-track interval buttons (seconds)
GLOBAL_PRESETS = (5, 10, 30, 60, 120, 300)            # default-interval buttons (seconds)
HUNT_MIN = float(os.environ.get("SMSCENTER_MIN_HUNT_INTERVAL") or 0.05)    # fastest a hunter may ask: 20 requests a second
PRICE_CHECK = 3.0                                       # seconds between price checks while "stop if the price changes" is on
MAX_HUNTERS_ON = 5
CODE_POLL = 2.0                                         # seconds between code checks for a bought number
V0_URL = "https://smscenter.pro/stubs/handler_api.php"      # SmsCenter's v0 API: its getStatus tells waiting / code / CANCELLED (v1 can't)
BATCH_WINDOW = 1.0                                      # seconds to gather numbers into one checker message
CHECK_TIMEOUT = 200                                     # seconds to wait for the checker bot before calling a number "not checked"
CODE_WAIT = 30 * 60                                     # give up waiting for a code after this long
HIST_KEEP = 200
NOMONEY_WAIT = 120.0                                    # a released number's refund can take a while: wait this long for the balance to come back
HUNT_FATAL = {"apiKeyNotFound": "SmsCenter rejected your API key",
              "noCountry": "SmsCenter doesn't know this country", "noService": "this platform isn't offered by SmsCenter",
              "badService": "this platform isn't offered here", "noOperator": "this operator isn't offered in this country",
              "badData": "SmsCenter rejected the request"}
WATCH_MIN = 15.0                                        # a price watch scans ~250 countries each time, so it can't be as fast as a single track
WATCH_PRESETS = (30, 60, 120, 300, 600)
WATCH_CONC = 8                                          # parallel country requests during a scan
WATCH_LINES = 40                                        # countries per message block
BASIS = {"api": "API price", "min": "Cheapest tier"}

# Platform codes are the usual SMS-activation codes. Unknown ones found in the API answer are added automatically, and you can
# also type any code yourself in "Search platform".
DEFAULT_SERVICES = {
    "tg": "Telegram", "wa": "WhatsApp", "fb": "Facebook", "ig": "Instagram", "go": "Google / Gmail", "tw": "Twitter / X",
    "ds": "Discord", "vi": "Viber", "wb": "WeChat", "lf": "TikTok", "ub": "Uber", "mm": "Microsoft", "ya": "Yandex",
    "vk": "VKontakte", "ok": "Odnoklassniki", "me": "Line", "am": "Amazon", "ni": "Gojek", "ka": "Shopee", "ot": "Any other",
}
log = logging.getLogger("smscenter_bot")
_SECRET_RE = re.compile(r"(?i)((?:api_?key|token)=)[^&\s'\"]+|(/bot\d+:)[\w-]+")


def redact(s: Any) -> str:
    """Strip API keys / tokens out of anything that is logged or sent to Telegram (URLs with ?apiKey=..., /bot<token>/...)."""
    return _SECRET_RE.sub(lambda m: (m.group(1) or m.group(2)) + "***", str(s))


class RedactFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


KB = list[list[tuple[str, str]]]
Reply = tuple[str, "KB | None"]


# ── secrets: Windows DPAPI when available (tied to your Windows account) ────────
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    inp, out = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI call failed")
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


_FLAG_FIX = {"uk": "gb"}


def flag(code: str) -> str:
    c = _FLAG_FIX.get(str(code).lower(), str(code).lower())
    if re.fullmatch(r"[a-z]{2}", c):
        return chr(0x1F1E6 + ord(c[0]) - 97) + chr(0x1F1E6 + ord(c[1]) - 97)
    return "🌐"


def fmt(v: float | None) -> str:
    return "-" if v is None else (f"{v:.4f}".rstrip("0").rstrip("."))


def px(v: float | None) -> str:
    """Price for display: at least 2 decimals, up to 4 (2 -> 2.00, 1.5 -> 1.50, 0.5334 -> 0.5334)."""
    if v is None:
        return "—"
    t = f"{v:.4f}".rstrip("0")
    if t.endswith("."):
        t += "00"
    elif len(t.split(".")[1]) < 2:
        t += "0"
    return t


def usd(v: float | None) -> str:
    return "—" if v is None else "$" + px(v)


def qty(v) -> str:
    return f"{int(v or 0):,}"


def mmss(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}m {sec % 60:02d}s"


def qty_short(v) -> str:
    n = int(v or 0)
    return f"{n / 1e6:.1f}M" if n >= 1_000_000 else f"{n / 1e3:.1f}K" if n >= 10_000 else f"{n:,}"


def pre(lines: list[str]) -> str:
    return "<pre>" + html.escape("\n".join(lines)) + "</pre>"


def secs(v: float) -> str:
    """5 -> '5s', 120 -> '2m', 90 -> '90s', 0.5 -> '0.5s'."""
    if v >= 60 and v % 60 == 0:
        return f"{int(v // 60)}m"
    return f"{v:g}s"


def parse_secs(text: str) -> float | None:
    """'30' / '30s' -> 30, '2m' -> 120, '1h' -> 3600, '0.5' -> 0.5. None = not understood."""
    t = text.strip().lower().replace(",", ".")
    mult = 1
    for suf, m in (("h", 3600), ("m", 60), ("s", 1)):
        if t.endswith(suf):
            t, mult = t[:-1].strip(), m
            break
    try:
        v = float(t) * mult
    except ValueError:
        return None
    return v if v > 0 else None


def _num(v) -> float | None:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def tkey(svc: str, cc: str) -> str:
    return f"{svc}:{cc}"


# ── persistent state ─────────────────────────────────────────────────────────────
def new_track(svc: str, cc: str, name: str, sname: str) -> dict:
    return {"svc": svc, "cc": cc, "name": name, "sname": sname, "on": False, "interval": None,
            "any": False, "drop": None, "under": None, "armed": True, "off": False}


class PhoneChecker:
    """Checks one phone number with the Telegram checker bot, through a Telethon user session found in this folder.
    One connection for the whole bot (a .session file can only be open once). Never raises: problems come back as ('off', reason) / ('unknown', reason)."""

    def __init__(self):
        self.bc = None
        self.queue: list[tuple[str, asyncio.Future]] = []
        self.worker: asyncio.Task | None = None
        self.err = ""
        self.retry_at = 0.0

    @staticmethod
    def session_path() -> str | None:
        env = os.environ.get("SMSCENTER_TG_SESSION", "").strip()
        if env and os.path.exists(env if env.endswith(".session") else env + ".session"):
            return env
        files = sorted((f for f in os.listdir(HERE) if f.endswith(".session")), key=lambda f: os.path.getmtime(os.path.join(HERE, f)), reverse=True)
        return os.path.join(HERE, files[0]) if files else None

    @staticmethod
    def creds() -> tuple[int, str, str] | None:
        """(api_id, api_hash, checker bot) from the environment, else from the NumberHub app's settings file (same machine, same Windows user)."""
        api_id, api_hash, bot = (os.environ.get(k, "").strip().lstrip("﻿").strip() for k in ("SMSCENTER_TG_API_ID", "SMSCENTER_TG_API_HASH", "SMSCENTER_CHECK_BOT"))
        if not (api_id and api_hash):
            try:
                with open(os.path.join(HERE, ".numberhub_settings.json"), encoding="utf-8") as f:
                    d = json.load(f)
                for pre_ in ("bot_", "tg_"):
                    if d.get(pre_ + "api_id") and d.get(pre_ + "api_hash"):
                        api_id = str(d[pre_ + "api_id"])
                        api_hash = _dpapi(base64.b64decode(d[pre_ + "api_hash"]), False).decode()
                        break
                bot = bot or d.get("bot_user") or ""
            except Exception:                                            # noqa: BLE001
                return None
        if not (api_id and api_hash):
            return None
        return int(api_id), api_hash, bot or "tgchecker3bababot"

    def describe(self) -> str:
        sp = self.session_path()
        return f"session {os.path.basename(sp)}" if sp else "no .session file in the scripts folder"

    async def close(self) -> None:
        bc, self.bc = self.bc, None
        if bc is not None:
            try:
                await bc.close()
            except Exception:                                            # noqa: BLE001
                pass

    async def check(self, phone: str) -> tuple[str, str]:
        """-> (verdict, note): clean | registered | banned | unknown (asked, no usable answer) | off (couldn't ask at all).
        Numbers that arrive within a moment of each other travel in ONE message (the checker bot takes 8 per request and only one request at a time)."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.queue.append((phone, fut))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._work())
        return await fut

    async def _work(self) -> None:
        await asyncio.sleep(BATCH_WINDOW)                                # a short gather window; while a request runs, the next numbers pile up for free
        while self.queue:
            batch, self.queue = self.queue[:8], self.queue[8:]
            try:
                res = await self._run_batch([p for p, _f in batch])
            except Exception as e:                                       # noqa: BLE001
                res = {p: ("unknown", f"{type(e).__name__}: {str(e)[:80]}") for p, _f in batch}
            for p, f in batch:
                if not f.done():
                    f.set_result(res.get(p, ("unknown", "no answer")))

    async def _run_batch(self, phones: list[str]) -> dict[str, tuple[str, str]]:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        from tgbot_checker import norm
        if self.bc is None:
            if time.time() < self.retry_at:
                return {p: ("off", self.err) for p in phones}
            sp, cr = self.session_path(), self.creds()
            if not sp:
                self.err, self.retry_at = "no .session file in the scripts folder", time.time() + 20
                return {p: ("off", self.err) for p in phones}
            if not cr:
                self.err, self.retry_at = "no Telegram api id/hash (set it once in the NumberHub app: Checker bot…)", time.time() + 20
                return {p: ("off", self.err) for p in phones}
            try:
                if HERE not in sys.path:
                    sys.path.insert(0, HERE)
                from tgbot_checker import BotChecker
                bc = BotChecker(sp, cr[0], cr[1], cr[2])
                await bc.open()
                self.bc = bc
            except Exception as e:                                       # noqa: BLE001
                self.err, self.retry_at = f"Telegram session problem: {type(e).__name__}: {str(e)[:80]}", time.time() + 60
                log.warning("checker: %s", self.err)
                return {p: ("off", self.err) for p in phones}
        try:
            res = await self.bc.check(phones)
            return {p: res.get(norm(p), ("unknown", "no answer")) for p in phones}
        except Exception as e:                                           # noqa: BLE001
            await self.close()                                           # reconnect next time
            return {p: ("unknown", f"{type(e).__name__}: {str(e)[:80]}") for p in phones}


CHECKER = PhoneChecker()


def digits(v) -> str:
    return re.sub(r"\D", "", str(v))


def parse_prefixes(text: str) -> list[str]:
    """'+38067, 68 073' -> ['+38067', '68', '073'];  'off' / 'none' / 'any' -> []"""
    out = []
    for tok in re.split(r"[,\s;]+", text.strip()):
        if not tok:
            continue
        if tok.lower() in ("off", "none", "any", "no", "-", "all"):
            return []
        t = re.sub(r"[^\d+]", "", tok)
        if digits(t):
            out.append(t)
    return out


def matches_prefix(tel, prefixes: list[str]) -> bool:
    """+380 67 = the whole number starts with it. 67 or 067 = the national part (after the 1-3 digit country code) starts with it.
    A long plain prefix (6+ digits, e.g. 380671) is also tried against the whole number."""
    if not prefixes:
        return True
    d = digits(tel)
    for p in prefixes:
        raw = p.lstrip("+")
        if p.startswith("+"):
            if d.startswith(raw):
                return True
            continue
        rest = raw[1:] if raw.startswith("0") else raw
        if rest and any(d[k:].startswith(rest) for k in (1, 2, 3)):
            return True
        if len(raw) >= 6 and not raw.startswith("0") and d.startswith(raw):
            return True
    return False


def number_ok(tel, include: list[str], exclude: list[str]) -> bool:
    """Kept only if it matches an 'only these' prefix (when there are any) and matches none of the 'not these' prefixes."""
    return matches_prefix(tel, include) and not (exclude and matches_prefix(tel, exclude))


def new_hunter(hid: str, svc: str, sname: str, cc: str, name: str) -> dict:
    return {"id": hid, "svc": svc, "sname": sname, "cc": cc, "name": name, "op": None, "prefix": [], "xprefix": [], "maxp": None, "want": 1,
            "interval": 1.0, "minutes": 15.0, "until": "time", "stopchg": False, "check": True, "on": False, "got": 0, "tries": 0, "rejected": 0, "last": ""}


def new_watch(wid: str, svc: str, sname: str, limit: float) -> dict:
    return {"id": wid, "svc": svc, "sname": sname, "limit": limit, "basis": "api", "interval": None, "on": True,
            "seeded": False, "under": {}, "names": {}, "last_scan": 0.0, "minstock": 1}


PROFILE_DEFAULTS: dict[str, Any] = {"api_key": "", "interval": 60, "drop_pct": 5.0, "paused": False, "service": DEFAULT_SVC,
                                    "endpoints": list(ENDPOINTS), "tracks": {}, "last": {}, "hist": {}, "mute_until": 0.0, "last_poll": 0.0,
                                    "watches": {}, "watch_interval": 120.0, "hunters": {}, "history": [],
                                    "mail": {"domain": "", "gmail": "", "app_password": "", "n": 0, "generated": []}}


def normalize(d: dict) -> dict:
    """Fill in one user's profile in place; also migrates the old Telegram-only layout ("countries") to tracks."""
    for k, v in PROFILE_DEFAULTS.items():
        d.setdefault(k, json.loads(json.dumps(v)))
    for k in ("feeds", "targets", "restock"):
        d.pop(k, None)                                                                             # old versions' keys
    old = d.pop("countries", None)
    if isinstance(old, dict):
        for cc, c in old.items():
            if not isinstance(c, dict):
                continue
            if isinstance(c.get("drop"), (int, float)):
                c["drop"] = {"pct": float(c["drop"])}
            t = new_track("tg", cc, c.get("name") or cc.upper(), "Telegram")
            t.update({k: v for k, v in c.items() if k in t and k not in ("svc", "cc", "sname")})
            t["on"] = True
            d["tracks"].setdefault(tkey("tg", cc), t)
        for bucket in ("last", "hist"):
            d[bucket] = {(k if ":" in k else tkey("tg", k)): v for k, v in d[bucket].items()}
    for key, t in list(d["tracks"].items()):
        if not isinstance(t, dict) or ":" not in key:
            d["tracks"].pop(key)
            continue
        svc, cc = key.split(":", 1)
        for k, v in new_track(svc, cc, cc.upper(), svc.upper()).items():
            t.setdefault(k, v)
    for hid, h in list(d["hunters"].items()):
        if not isinstance(h, dict):
            d["hunters"].pop(hid)
            continue
        for k, v in new_hunter(str(hid), h.get("svc", ""), h.get("sname", ""), h.get("cc", ""), h.get("name", "")).items():
            h.setdefault(k, v)
        h["on"] = False                                                  # a restart never resumes buying by itself
    if not isinstance(d["history"], list):
        d["history"] = []
    for wid, w in list(d["watches"].items()):
        if not isinstance(w, dict):
            d["watches"].pop(wid)
            continue
        for k, v in new_watch(str(wid), w.get("svc", ""), w.get("sname", ""), float(w.get("limit") or 0)).items():
            w.setdefault(k, v)
    d["last"] = {k: v for k, v in d["last"].items() if k in d["tracks"] and isinstance(v, dict) and "count" in v}
    return d


class Store:
    """The JSON file: bot token, admin, who may use the bot, and one profile per Telegram user (own key, tracks, settings)."""

    def __init__(self, path: str):
        self.path = path
        self._saved = 0.0
        self.d: dict[str, Any] = {"token": "", "admin": None, "access": "open", "allowed": [], "blocked": [], "users": {}, "legacy": None, "setup_done": False}
        try:
            with open(path, encoding="utf-8") as f:
                self.d.update(json.load(f))
        except (OSError, ValueError):
            pass
        # a file from the one-user versions: its settings become the profile of the owner it had (or of the first /start if it had none)
        legacy = {k: self.d.pop(k) for k in (*PROFILE_DEFAULTS, "countries", "feeds", "targets", "restock") if k in self.d}
        owner = self.d.pop("owner", None)
        if legacy:
            if owner:
                self.d["admin"] = self.d["admin"] or owner
                self.d["users"].setdefault(str(owner), legacy)
            else:
                self.d["legacy"] = legacy
        admin = _admin_id()
        if admin:                                                        # the configured admin always wins over the saved one
            self.d["admin"] = admin
            if self.d.get("legacy"):
                self.d["users"].setdefault(str(admin), self.d.pop("legacy"))
                self.d["legacy"] = None
        for prof in self.d["users"].values():
            normalize(prof)
        if self.d.get("legacy"):
            normalize(self.d["legacy"])

    @property
    def token(self) -> str:
        return os.environ.get("SMSCENTER_BOT_TOKEN", "").strip() or unseal(self.d["token"])      # a server sets the token in the environment

    def set_secret(self, key: str, value: str) -> None:
        self.d[key] = seal(value)
        self.save()

    def save(self, throttle: float = 0.0) -> None:
        if throttle and time.time() - self._saved < throttle:
            return
        self._saved = time.time()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    # -- catch-all e-mail: the admin's profile holds the one domain / Gmail / app password; generated addresses live in each user's profile
    def mail_cfg(self) -> tuple[str, str, str] | None:
        m = (self.d["users"].get(str(self.d["admin"])) or {}).get("mail") or {}
        return (m["gmail"], unseal(m.get("app_password", "")), m["domain"]) if (m.get("domain") and m.get("gmail") and m.get("app_password")) else None

    def mail_owners(self) -> dict[str, int]:
        """Every generated address -> the user it was made for."""
        return {r["addr"].lower(): int(k) for k, u in self.d["users"].items() if k.lstrip("-").isdigit() for r in (u.get("mail") or {}).get("generated", [])}


class UserState:
    """One user's view of the Store: .d is that user's profile (own SmsCenter key, tracks, intervals, alert settings)."""

    def __init__(self, store: Store, uid: int):
        self.store, self.uid = store, uid
        self.d: dict[str, Any] = normalize(store.d["users"].setdefault(str(uid), {}))

    @property
    def api_key(self) -> str:
        return unseal(self.d["api_key"])

    @property
    def mail(self) -> dict:
        m = self.d.setdefault("mail", {})
        for k, v in PROFILE_DEFAULTS["mail"].items():
            m.setdefault(k, json.loads(json.dumps(v)))
        return m

    @property
    def endpoints(self) -> list[str]:
        return [e for e in ENDPOINTS if e in self.d["endpoints"]] or ["offer"]

    def interval_of(self, t: dict) -> float:
        return max(MIN_INTERVAL, float(t.get("interval") or self.d["interval"]))

    def watch_interval_of(self, w: dict) -> float:
        return max(WATCH_MIN, float(w.get("interval") or self.d["watch_interval"]))

    def set_secret(self, key: str, value: str) -> None:
        self.d[key] = seal(value)
        self.save()

    def save(self, throttle: float = 0.0) -> None:
        self.store.save(throttle)


# ── SmsCenter API: the three price endpoints, for any platform ───────────────────
class SmsCenterError(Exception):
    pass


class KeepOpen:
    """Hands out the shared client but ignores close(), so menu taps reuse one warm connection instead of a new TLS handshake each time."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def close(self):
        pass


class SmsCenter:
    NOT_OFFERED = {"badService", "noService", "noCountry", "noOperator"}

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.http = httpx.AsyncClient(timeout=25.0, limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=120))

    async def close(self):
        await self.http.aclose()

    async def get(self, path: str, **params):
        # their docs are inconsistent about the key name, so both are sent (same trick as shared/adapters/smscenter.py)
        q = {"apiKey": self.api_key, "api_key": self.api_key, **{k: v for k, v in params.items() if v not in (None, "")}}
        try:
            r = await self.http.get(BASE_URL + path, params=q)
            data = r.json()
        except httpx.HTTPError as e:
            raise SmsCenterError(f"network error: {redact(e)}") from e
        except ValueError:
            raise SmsCenterError(f"bad response (HTTP {r.status_code})")
        if isinstance(data, dict) and data.get("error"):
            raise SmsCenterError(str(data["error"]))
        return data

    async def countries(self) -> list[dict]:
        data = await self.get("/getCountryList")
        return data if isinstance(data, list) else []

    async def _maybe(self, path: str, **params):
        """None when SmsCenter says the platform/country isn't offered here; real problems (bad key, network) still raise."""
        try:
            return await self.get(path, **params)
        except SmsCenterError as e:
            if str(e) in self.NOT_OFFERED:
                return None
            raise

    async def service_codes(self, cc: str) -> list[str]:
        """Platform codes that getCountNumbersList returns for a country (used to discover platforms beyond the built-in list)."""
        data = await self._maybe("/getCountNumbersList", country=cc)
        if not isinstance(data, dict):
            return []
        return [str(k).lower() for k, v in data.items() if isinstance(v, (list, dict)) and re.fullmatch(r"[A-Za-z0-9_]{1,16}", str(k))]

    async def all_services(self, cc: str) -> dict | None:
        """getCountNumbersList for a country: EVERY platform's price in one call (what a price-watch sweep uses)."""
        data = await self._maybe("/getCountNumbersList", country=cc)
        return data if isinstance(data, dict) else None

    async def offer(self, svc: str, cc: str) -> dict | None:
        """getOfferNumberList -> {"min", "api", "stock", "tiers": [(price, count), ...]}"""
        return parse_offer(svc, await self._maybe("/getOfferNumberList", country=cc, services=svc))

    async def numbers_list(self, svc: str, cc: str, operator: str | None = None) -> dict | None:
        """getCountNumbersList -> {"price", "qty", "tiers": [(cost, qty), ...]}  (with `operator`: that operator's own price + stock)"""
        return parse_list(svc, await self._maybe("/getCountNumbersList", country=cc, operator=operator))

    async def get_number(self, svc: str, cc: str, operator: str | None = None, max_price: float | None = None) -> dict:
        """getNumber -> {"tel", "idNum", "price"}. Raises SmsCenterError("noNumber" / "noMoney" / ...) when nothing was bought."""
        data = await self.get("/getNumber", service=svc, country=cc, operator=operator, maxPrice=max_price)
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict) or not data.get("tel") or not data.get("idNum"):
            raise SmsCenterError("badResponse")
        return {"tel": str(data["tel"]), "idNum": str(data["idNum"]), "price": _num(data.get("price"))}

    async def balance(self) -> float | None:
        data = await self.get("/getBalance")
        return _num(data.get("balance")) if isinstance(data, dict) else None

    async def v0_status(self, id_num: str) -> tuple[str, str | None]:
        """getStatus of the v0 API (same key, same id) -> ('wait', None) | ('ok', code) | ('cancel', None) | ('?', None).
        'cancel' = SmsCenter ended it: it timed out, or it was cancelled (here or from the website panel)."""
        try:
            r = await self.http.get(V0_URL, params={"api_key": self.api_key, "action": "getStatus", "id": id_num})
        except httpx.HTTPError as e:
            raise SmsCenterError(f"network error: {redact(e)}") from e
        t = r.text.strip()
        if t.startswith("STATUS_OK"):
            return "ok", (t.split(":", 1)[1].strip() if ":" in t else None) or None
        if t.startswith("STATUS_CANCEL"):
            return "cancel", None
        if t.startswith("STATUS_WAIT"):
            return "wait", None
        return "?", None

    async def sms_code(self, id_num: str) -> str | None:
        """getSmsCode -> the code, or None while nothing has arrived yet."""
        try:
            data = await self.get("/getSmsCode", idNum=id_num)
        except SmsCenterError as e:
            if str(e) in ("idNumNotFound", "apiKeyNotFound", "badData"):
                raise
            return None                                                  # "no code yet" is not documented as a distinct answer: keep polling
        c = data.get("smsCode") if isinstance(data, dict) else None
        if isinstance(c, list):
            c = c[-1] if c else None
        return str(c) if c else None

    async def set_status(self, id_num: str, status: str) -> str:
        """setStatus end = cancel/release. Answers 'ready' (done), 'smsReceived' / 'waitSms' (too late), 'update'."""
        data = await self.get("/setStatus", idNum=id_num, status=status)
        return str(data.get("status")) if isinstance(data, dict) else ""

    async def count(self, svc: str, cc: str) -> dict | None:
        """getCountNumber -> {"count", "price"}"""
        return parse_count(svc, await self._maybe("/getCountNumber", service=svc, country=cc, price="true"))


def parse_offer(svc: str, data) -> dict | None:
    o = data.get(svc) if isinstance(data, dict) else None
    if not isinstance(o, dict):
        return None
    tiers = []
    for p, c in (o.get("priceMap") or {}).items():
        if _num(p):
            tiers.append((float(p), int(_num(c) or 0)))
    tiers.sort()
    # tier counts are cumulative ("how many numbers cost this much OR LESS"), and minPrice can be a tier with 0 numbers (live: jp),
    # so "cheapest" = the lowest tier that really has stock
    in_stock = next((p for p, q in tiers if q > 0), None)
    return {"min": in_stock or _num(o.get("minPrice")), "api": _num(o.get("apiPrice")), "stock": int(_num(o.get("totalCount")) or 0), "tiers": tiers}


def parse_list(svc: str, data) -> dict | None:
    v = data.get(svc) if isinstance(data, dict) else None
    if isinstance(v, dict):                                              # what the docs say; the live API returns a list
        v = [v]
    if not isinstance(v, list) or not v:
        return None
    tiers = sorted((float(x["cost"]), int(_num(x.get("quantity")) or 0)) for x in v if isinstance(x, dict) and _num(x.get("cost")))
    if not tiers:
        return None
    return {"price": tiers[0][0], "qty": tiers[0][1], "tiers": tiers}


def parse_count(svc: str, data) -> dict | None:
    if not isinstance(data, dict) or svc not in data:
        return None
    return {"count": int(_num(data.get(svc)) or 0), "price": _num(data.get("price"))}


async def fetch_country(client, svc: str, cc: str, endpoints: list[str]) -> dict[str, dict | None]:
    """{"offer": {...}|None, "list": {...}|None, "count": {...}|None} - only the enabled endpoints are called (in parallel)."""
    names = [e for e in ("offer", "list", "count") if e in endpoints]
    calls = {"offer": client.offer, "list": client.numbers_list, "count": client.count}
    res = await asyncio.gather(*(calls[n](svc, cc) for n in names))
    return dict(zip(names, res))


def snapshot(raw: dict[str, dict | None]) -> dict | None:
    """Flatten the three answers into the prices we compare. None = this platform isn't offered in this country."""
    o, l, c = raw.get("offer"), raw.get("list"), raw.get("count")
    if not (o or l or c):
        return None
    return {"min": (o or {}).get("min"), "api": (o or {}).get("api"), "list": (l or {}).get("price"), "count": (c or {}).get("price"),
            "stock": max((o or {}).get("stock") or 0, (l or {}).get("qty") or 0, (c or {}).get("count") or 0)}


PRICE_KEYS = ("min", "api", "list", "count")
API_KEYS = ("api", "list", "count")                      # the three endpoints' view of the "API price"


def cheapest(s: dict) -> float | None:
    """Lowest price you can actually buy at: the offer endpoint's cheapest tier WITH stock, else the other endpoints' price if anything is in stock."""
    if s.get("min"):
        return s["min"]
    vals = [s[k] for k in API_KEYS if s.get(k)]
    return min(vals) if vals and s.get("stock") else None


# ── per-track settings ───────────────────────────────────────────────────────────
def parse_drop(text: str) -> tuple[str, Any] | None:
    """'0.05' -> amount, '10%' -> percent, 'off' / 'default'. None = not understood."""
    t = text.strip().lower().replace(",", ".")
    if t in ("off", "no", "none"):
        return "off", None
    if t in ("default", "reset"):
        return "default", None
    try:
        if t.endswith("%"):
            v = float(t[:-1])
            return ("pct", v) if 0 < v <= 100 else None
        v = float(t)
        return ("amt", v) if v > 0 else None
    except ValueError:
        return None


def drop_text(c: dict, default_pct: float) -> str:
    d = c.get("drop")
    if d == "off":
        return "off"
    if not d:
        return f"{default_pct:g}% (default)"
    parts = []
    if d.get("amt"):
        parts.append(f"{fmt(d['amt'])}")
    if d.get("pct"):
        parts.append(f"{d['pct']:g}%")
    return " or ".join(parts) or f"{default_pct:g}% (default)"


# ── the tracker (no Telegram in here, so it can be tested) ───────────────────────
Notify = Callable[[str], Awaitable[None]]


class Tracker:
    def __init__(self, state: UserState, client_factory: Callable[[], SmsCenter], notify: Notify):
        self.s, self.make_client, self.notify = state, client_factory, notify
        self.wake = asyncio.Event()
        self.force = False
        self.next: dict[str, float] = {}                                 # track key -> when it is due again
        self.err_streak = ""
        self.wnext: dict[str, float] = {}                                # watch id -> when it is due again
        self.werr = ""
        self._cc: list[dict] = []
        self._cc_at = 0.0

    def poke(self, force: bool = False) -> None:
        self.force = self.force or force
        self.wake.set()

    def muted(self) -> bool:
        return time.time() < float(self.s.d.get("mute_until") or 0)

    def changes(self, old: dict, new: dict) -> list[tuple[str, float, float]]:
        return [(k, old[k], new[k]) for k in PRICE_KEYS if old.get(k) and new.get(k) and old[k] != new[k]]

    def wanted(self, c: dict, ch: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
        """Which of the changes this track's settings want to hear about."""
        if c.get("any"):
            return ch
        d = c.get("drop")
        if d == "off":
            return []
        amt, pct = (d or {}).get("amt"), (d or {}).get("pct")
        if not amt and not pct:
            pct = float(self.s.d["drop_pct"])
        out = []
        for k, a, b in ch:
            if b < a and ((amt and a - b >= amt - 1e-9) or (pct and (a - b) / a * 100 >= pct - 1e-9)):
                out.append((k, a, b))
        return out

    ENDP = {"min": "offer", "api": "offer", "list": "list", "count": "count"}

    def format_alert(self, t: dict, new: dict, ch: list[tuple[str, float, float]]) -> str:
        rows, note = [], ""
        api = [x for x in ch if x[0] in API_KEYS]
        for k, a_, b_ in ch:
            if k == "min":
                rows.append(("Cheapest", a_, b_))
        if api and len({(round(a_, 6), round(b_, 6)) for _, a_, b_ in api}) == 1 and len(api) > 1:
            rows.append(("API price", api[0][1], api[0][2]))             # several endpoints moved together: one row
            note = "\n<i>Confirmed by: " + ", ".join(ENDPOINTS[self.ENDP[k]] for k, _, _ in api) + "</i>"
        else:
            rows.extend((f"API price [{self.ENDP[k]}]", a_, b_) for k, a_, b_ in api)
        down, up = any(b_ < a_ for _, a_, b_ in rows), any(b_ > a_ for _, a_, b_ in rows)
        title = "Price decrease" if down and not up else "Price increase" if up and not down else "Price change"
        icon = "📉" if down and not up else "📈" if up and not down else "🔁"
        body = [f"{lbl:<20}{usd(a_):>9} → {usd(b_):<9} {self._pct(a_, b_)}" for lbl, a_, b_ in rows] + [f"{'Stock':<20}{qty(new.get('stock'))}"]
        return f"{icon} <b>{title}</b> · {flag(t['cc'])} {html.escape(t['name'])} · {html.escape(t['sname'])}\n{pre(body)}{note}"

    @staticmethod
    def _pct(a, b) -> str:
        return f"{'−' if b < a else '+'}{abs(b - a) / a * 100:.0f}%"

    async def poll_track(self, client, key: str) -> list[str]:
        t = self.s.d["tracks"].get(key)
        if t is None:
            return []
        raw = await fetch_country(client, t["svc"], t["cc"], self.s.endpoints)
        new = snapshot(raw)
        out: list[str] = []
        if new is None:
            return out                                                   # not offered here right now: nothing to compare
        old = self.s.d["last"].get(key)
        if old is not None:
            ch = self.changes(old, new)
            want = self.wanted(t, ch)
            if want and not t.get("off") and not self.muted():
                out.append(self.format_alert(t, new, want))
            u = t.get("under")
            cur = cheapest(new)
            if u and cur is not None:
                if cur <= u and t.get("armed", True) and not t.get("off"):
                    t["armed"] = False
                    out.append(f"🎯 <b>Price limit reached</b> · {flag(t['cc'])} {html.escape(t['name'])} · {html.escape(t['sname'])}\n"
                               + pre([f"{'Current price':<20}{usd(cur):>9}", f"{'Your limit':<20}{usd(u):>9}", f"{'Stock':<20}{qty(new['stock'])}"]))
                elif cur > u:
                    t["armed"] = True
        h = self.s.d["hist"].setdefault(key, [])
        if old is None or any(old.get(k) != new.get(k) for k in PRICE_KEYS):
            h.append([int(time.time()), new["min"], new["api"], new["list"], new["count"], new["stock"]])
            del h[:-HIST_MAX]
        self.s.d["last"][key] = new
        return out

    # ---- price watch: every country of one platform vs your price
    async def country_list(self, client) -> list[dict]:
        if not self._cc or time.time() - self._cc_at > 3600:
            raw = await client.countries()
            self._cc = [{"code": str(x["countryCode"]).lower(), "name": str(x.get("countryName") or x["countryCode"])} for x in raw if x.get("countryCode")]
            self._cc_at = time.time()
        return self._cc

    @staticmethod
    def watch_lines(names: dict[str, str], rows: list[tuple[str, str]]) -> str:
        return "<pre>" + "\n".join(f"{flag(cc)} {html.escape(names.get(cc, cc.upper())[:16]):<16} {text}" for cc, text in rows) + "</pre>"

    async def scan_watch(self, client, w: dict, cs: list[dict], cache: dict) -> list[str]:
        """One scan of every country for one watch. Returns the messages to send (first scan: the full list; later: only the changes)."""
        svc, limit, basis = w["svc"], float(w["limit"]), w.get("basis", "api")
        minstock = max(1, int(w.get("minstock") or 1))
        sem = asyncio.Semaphore(WATCH_CONC)
        prices: dict[str, tuple[float | None, int]] = {}                 # country -> (price, stock)
        failed: set[str] = set()

        async def one(cc: str):
            async with sem:
                try:
                    if basis == "min":
                        o = await client.offer(svc, cc)
                        tiers = (o or {}).get("tiers") or []
                        if o and minstock > 1 and tiers:                 # the cheapest tier that has at least `minstock` numbers behind it
                            price = next((pr for pr, q in tiers if q >= minstock), None)
                            prices[cc] = (price, dict(tiers).get(price, 0) if price else 0)
                        else:
                            prices[cc] = ((o or {}).get("min"), (o or {}).get("stock") or 0) if o else (None, 0)
                    else:
                        if cc not in cache:
                            cache[cc] = await client.all_services(cc)
                        l = parse_list(svc, cache[cc])
                        prices[cc] = (l["price"], l["qty"]) if l else (None, 0)
                except SmsCenterError:
                    failed.add(cc)

        await asyncio.gather(*(one(c["code"]) for c in cs))
        if len(failed) > max(10, len(cs) // 4):
            raise SmsCenterError(f"{len(failed)} of {len(cs)} countries failed to load - will retry")
        names = {c["code"]: c["name"] for c in cs}
        now = {cc: p for cc, (p, st) in prices.items() if p is not None and st >= minstock and p <= limit + 1e-9}
        prev = w.get("under") or {}
        w["last_scan"] = time.time()
        title = f"<b>{html.escape(w['sname'])}</b>"
        out: list[str] = []
        if not w.get("seeded"):                                          # FIRST scan: the whole list
            w["seeded"], w["under"], w["names"] = True, now, {cc: names.get(cc, cc.upper()) for cc in now}
            if not now:
                return [f"🎯 {title} · no country is under <b>{usd(limit)}</b> right now.\nI'll tell you the moment one drops under."]
            rows = sorted(now.items(), key=lambda kv: (kv[1], names.get(kv[0], kv[0])))
            for i in range(0, len(rows), WATCH_LINES):
                head = (f"🎯 {title} · countries under <b>{usd(limit)}</b> ({BASIS[basis]})\n<b>{len(rows)}</b> right now. From now on I only message you when something changes."
                        if i == 0 else f"🎯 {title} · under {usd(limit)} (continued)")
                out.append(head + "\n" + self.watch_lines(names, [(cc, usd(p)) for cc, p in rows[i:i + WATCH_LINES]]))
            return out
        for cc in failed:
            if cc in prev:
                now[cc] = prev[cc]                                       # a country that failed to load keeps what we knew: no invented changes
        new = sorted(((cc, now[cc]) for cc in now if cc not in prev), key=lambda kv: kv[1])
        drops = sorted(((cc, prev[cc], now[cc]) for cc in now if cc in prev and now[cc] < prev[cc] - 1e-9), key=lambda t: t[2])
        rises = sorted(((cc, prev[cc], now[cc]) for cc in now if cc in prev and now[cc] > prev[cc] + 1e-9), key=lambda t: t[2])
        gone = []
        for cc in prev:
            if cc in now:
                continue
            p, st = prices.get(cc, (None, 0))
            gone.append((cc, prev[cc], f"now {usd(p)}" if (p is not None and st >= minstock) else "sold out / too little stock"))
        w["under"] = now
        w["names"] = {**w.get("names", {}), **{cc: names.get(cc, cc.upper()) for cc in now}}
        if not (new or drops or rises or gone) or self.muted():
            return []
        sections: list[tuple[str, list[tuple[str, str]]]] = []
        if new:
            sections.append(("🆕 <b>Now under</b>", [(cc, usd(p)) for cc, p in new]))
        if drops:
            sections.append(("📉 <b>Dropped</b>", [(cc, f"{usd(a)} → {usd(b)} ({self._pct(a, b)})") for cc, a, b in drops]))
        if rises:
            sections.append(("📈 <b>Went up</b> (still under)", [(cc, f"{usd(a)} → {usd(b)} ({self._pct(a, b)})") for cc, a, b in rises]))
        if gone:
            sections.append(("🚫 <b>No longer under</b>", [(cc, f"was {usd(a)} · {why}") for cc, a, why in gone]))
        head = f"🔔 {title} · under <b>{usd(limit)}</b> · changes"
        msg = head
        for ttl, rows in sections:
            for i in range(0, len(rows), WATCH_LINES):
                block = f"\n{ttl}\n" + self.watch_lines(names, rows[i:i + WATCH_LINES])
                if len(msg) + len(block) > 3500:
                    out.append(msg)
                    msg = head + " (continued)"
                msg += block
        out.append(msg)
        return out

    async def watch_cycle(self, force: bool = False) -> int:
        """Scan every watch that is due. Returns how many messages were sent."""
        if not self.s.api_key:
            return 0
        now = time.time()
        due = [w for w in self.s.d["watches"].values() if w.get("on") and (force or self.wnext.get(w["id"], 0) <= now)]
        if not due:
            return 0
        for w in due:
            self.wnext[w["id"]] = now + self.s.watch_interval_of(w)
        client = self.make_client()
        cache: dict[str, Any] = {}                                       # raw per-country answers, shared by every 'API price' watch this round
        sent, errors = 0, []
        try:
            cs = await self.country_list(client)
            for w in due:
                try:
                    for text in await self.scan_watch(client, w, cs, cache):
                        await self.notify(text)
                        sent += 1
                except SmsCenterError as e:
                    errors.append(f"{w['sname']}: {e}")
                except Exception:                                        # noqa: BLE001
                    log.exception("watch %s failed", w.get("id"))
        except SmsCenterError as e:
            errors.append(str(e))
        finally:
            await client.close()
        self.s.save()
        if errors:
            msg = "; ".join(errors)[:300]
            if msg != self.werr:
                self.werr = msg
                await self.notify("⚠️ Price watch error: " + html.escape(msg) + "\nI keep retrying.")
        elif self.werr:
            self.werr = ""
            await self.notify("✅ Price watch is working again.")
        return sent

    def due_in(self) -> float:
        """Seconds until the next track or watch is due (so a 1-second track really is checked every second)."""
        now = time.time()
        waits = [self.next.get(k, 0) - now for k, t in self.s.d["tracks"].items() if t.get("on")]
        waits += [self.wnext.get(w["id"], 0) - now for w in self.s.d["watches"].values() if w.get("on")]
        return min(5.0, max(0.2, min(waits))) if waits else 2.0

    async def cycle(self, force: bool = False) -> tuple[int, int]:
        """Check every active track that is due. Returns (tracks ok, messages sent)."""
        if not self.s.api_key:
            return 0, 0
        now = time.time()
        tracks = self.s.d["tracks"]
        due = [k for k, t in tracks.items() if t.get("on") and (force or self.next.get(k, 0) <= now)]
        if not due:
            return 0, 0
        for k in due:
            self.next[k] = now + self.s.interval_of(tracks[k])
        client = self.make_client()
        sem = asyncio.Semaphore(4)
        ok, sent, errors = [0], [0], []

        async def one(key: str):
            async with sem:
                try:
                    for text in await self.poll_track(client, key):
                        await self.notify(text)
                        sent[0] += 1
                    ok[0] += 1
                except SmsCenterError as e:
                    errors.append(f"{key}: {e}")
                except Exception:                                        # noqa: BLE001
                    log.exception("poll %s failed", key)

        try:
            await asyncio.gather(*(one(k) for k in due))
        finally:
            await client.close()
        self.s.d["last_poll"] = time.time()
        self.s.save(throttle=0.0 if sent[0] else 5.0)
        if errors:
            msg = "; ".join(errors)[:300]
            if msg != self.err_streak:                                   # tell the owner once per kind of problem
                self.err_streak = msg
                await self.notify("⚠️ SmsCenter error: " + html.escape(msg) + "\nI keep retrying.")
        elif self.err_streak:
            self.err_streak = ""
            await self.notify("✅ SmsCenter is answering again.")
        return ok[0], sent[0]

    async def run(self):
        while True:
            force, self.force = self.force, False
            try:
                if not self.s.d["paused"]:
                    await self.cycle(force)
                    await self.watch_cycle(force)
            except Exception:                                            # noqa: BLE001
                log.exception("cycle failed")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.due_in())
            except asyncio.TimeoutError:
                pass
            self.wake.clear()


# ── the bot logic: commands + button screens (no aiogram in here either) ─────────
def _nav(*extra: tuple[str, str]) -> KB:
    return [list(extra), [("🏠 Menu", "m")]] if extra else [[("🏠 Menu", "m")]]


class App:
    HELP = ("<b>SmsCenter · price tracker (all platforms)</b>\nEvery user has their own SmsCenter key, tracks and intervals: start with /setkey.\nUse the buttons (/menu): <b>Platforms → country → ▶️ Start tracking</b>.\n"
            "Commands (<code>[platform]</code> = code or name like tg / telegram / wa; omit it to use the last one you opened):\n"
            "/platforms · /countries [platform]\n/prices [platform] &lt;country&gt; - live prices, all 3 endpoints\n"
            "/add [platform] &lt;country&gt; - start tracking · /stop · /remove · /list\n"
            "/set [platform] &lt;country&gt; any on|off\n/set [platform] &lt;country&gt; drop 0.05 | 10% | off | default\n"
            "/set [platform] &lt;country&gt; under 1.20 | off\n/set [platform] &lt;country&gt; every 10 (seconds) | default\n"
            "/trend [platform] &lt;country&gt; · /interval &lt;sec&gt; (default) · /drop &lt;%&gt; (default) · /mute &lt;min|off&gt;\n"
            "/watch [platform] &lt;price&gt; - tell me every country under that price, then only the changes · /watches · /unwatch &lt;id|all&gt;\n"
            "/hunter - buy numbers with a hunter (prefix, max price, how many, how often) · /history\n/setkey &lt;apikey&gt; · /check · /pause (monitoring only) · /resume · /status\nOr just type a country or platform name to find it.")

    def __init__(self, state: UserState, bot=None, uid: int = 0, client_factory=None):
        self.s, self.bot, self.uid = state, bot, uid
        self._custom_factory, self._ui_cl = client_factory is not None, None
        self.client_factory = client_factory or (lambda: SmsCenter(self.s.api_key))
        self.tracker = Tracker(state, self.client_factory, self.notify)
        self._countries: list[dict] = []
        self._services: dict[str, str] | None = None
        self.pending: dict[int, tuple[str, str, str]] = {}      # owner is typing a value: (field, platform, country)
        self.check_tasks: dict[str, set] = {}                   # hunter id -> number checks still running
        self.checker = CHECKER                                  # shared Telegram-checker (one Telethon session for the whole bot)
        self.hunts: dict[str, asyncio.Task] = {}                # hunter id -> running hunt loop
        self.code_tasks: dict[str, asyncio.Task] = {}           # bought number (idNum) -> waiting for its code
        self.hub: Hub | None = None                             # set by the Hub: owns the one catch-all mail watcher
        self.wipe_input = False                                 # the message just typed holds a secret: delete it

    async def notify(self, text: str):
        if not (self.uid and self.bot):
            return
        try:
            await self.bot.send_message(self.uid, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:                                           # noqa: BLE001
            log.warning("could not message %s: %s", self.uid, e)
            if any(w in str(e).lower() for w in ("blocked", "deactivated", "chat not found")):
                self.s.d["paused"] = True                                # the user left: stop polling for them until they come back
                self.s.save()

    def ui_client(self):
        """The client for menu taps: one long-lived connection per API key (the key can change with /setkey)."""
        if self._custom_factory:
            return self.client_factory()
        key = self.s.api_key
        if self._ui_cl is None or self._ui_cl.api_key != key:
            self._ui_cl = SmsCenter(key)
        return KeepOpen(self._ui_cl)

    async def warm(self) -> None:
        """Right after start: load the country + platform lists so the first Hunter tap is instant."""
        try:
            if self.s.api_key:
                await self.load_services()
        except Exception:                                                # noqa: BLE001
            pass

    async def notify_kb(self, text: str, kb: "KB | None" = None, reply_to: int | None = None) -> int | None:
        """A message WITH buttons (optionally a reply to an earlier message). Returns its message id."""
        if not (self.uid and self.bot):
            return None
        try:
            kw = {}
            if reply_to:
                from aiogram.types import ReplyParameters
                kw["reply_parameters"] = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
            m = await self.bot.send_message(self.uid, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=to_markup(kb), **kw)
            return m.message_id
        except Exception as e:                                           # noqa: BLE001
            log.warning("could not message %s: %s", self.uid, e)
            return None

    async def edit_msg(self, mid: int | None, text: str, kb: "KB | None" = None) -> None:
        if not (self.uid and self.bot and mid):
            return
        try:
            await self.bot.edit_message_text(text, chat_id=self.uid, message_id=mid, parse_mode="HTML", disable_web_page_preview=True, reply_markup=to_markup(kb))
        except Exception as e:                                           # noqa: BLE001
            if "not modified" not in str(e).lower():
                log.warning("edit failed: %s", e)

    async def pin_msg(self, mid: int | None) -> None:
        if self.uid and self.bot and mid:
            try:
                await self.bot.pin_chat_message(chat_id=self.uid, message_id=mid, disable_notification=True)
            except Exception as e:                                       # noqa: BLE001
                log.warning("pin failed: %s", e)

    async def unpin_msg(self, mid: int | None) -> None:
        if self.uid and self.bot and mid:
            try:
                await self.bot.unpin_chat_message(chat_id=self.uid, message_id=mid)
            except Exception as e:                                       # noqa: BLE001
                log.warning("unpin failed: %s", e)

    def shutdown(self) -> None:
        for t in [*self.hunts.values(), *self.code_tasks.values(), *[x for v in self.check_tasks.values() for x in v]]:
            t.cancel()
        self.hunts.clear()
        self.code_tasks.clear()

    # -- countries + platforms
    async def load_countries(self) -> list[dict]:
        if not self._countries:
            c = self.ui_client()
            try:
                raw = await c.countries()
            finally:
                await c.close()
            self._countries = sorted(({"code": str(x["countryCode"]).lower(), "name": str(x.get("countryName") or x["countryCode"]),
                                       "ops": [str(o) for o in (x.get("operatorList") or []) if str(o).lower() != "any"]}
                                      for x in raw if x.get("countryCode")), key=lambda x: x["name"].lower())
        return self._countries

    async def load_services(self) -> dict[str, str]:
        """Built-in platforms + any extra codes SmsCenter returns. Cached once the API answered."""
        if self._services is None:
            svcs = dict(DEFAULT_SERVICES)
            if self.s.api_key:
                try:
                    cs = await self.load_countries()
                    probe = next((x["code"] for x in cs if x["code"] in ("us", "gb", "ru")), cs[0]["code"] if cs else None)
                    if probe:
                        c = self.ui_client()
                        try:
                            codes = await c.service_codes(probe)
                        finally:
                            await c.close()
                        for code in codes:
                            svcs.setdefault(code, code.upper())
                    self._services = svcs
                except SmsCenterError:
                    return svcs
            else:
                return svcs
        return self._services

    @staticmethod
    def find_service(text: str, svcs: dict[str, str], fuzzy: bool = True) -> str | None:
        t = text.strip().lower()
        if not t:
            return None
        if t in svcs:
            return t
        for code, name in svcs.items():
            if t == name.lower():
                return code
        if fuzzy:
            hits = [code for code, name in svcs.items() if t in name.lower()]
            if len(hits) == 1:
                return hits[0]
        return None

    async def resolve(self, text: str) -> dict | None:
        t = text.strip().lower()
        cs = await self.load_countries()
        for x in cs:
            if t in (x["code"], x["name"].lower()):
                return x
        hits = [x for x in cs if t and t in x["name"].lower()]
        return hits[0] if len(hits) == 1 else None

    def need_key(self) -> Reply | None:
        return None if self.s.api_key else ("Set your SmsCenter API key first:\n<code>/setkey YOUR_KEY</code>", _nav())

    # -- tracks
    async def ensure(self, svc: str, cc: str) -> dict:
        """The track for (platform, country), created (not started) if it doesn't exist yet."""
        key = tkey(svc, cc)
        t = self.s.d["tracks"].get(key)
        if t is None:
            svcs = await self.load_services()
            name = next((x["name"] for x in await self.load_countries() if x["code"] == cc), cc.upper())
            t = self.s.d["tracks"][key] = new_track(svc, cc, name, svcs.get(svc, svc.upper()))
            self.s.save()
        return t

    def set_on(self, svc: str, cc: str, on: bool) -> None:
        key = tkey(svc, cc)
        t = self.s.d["tracks"][key]
        t["on"] = on
        if on:
            self.s.d["last"].pop(key, None)                              # fresh baseline: don't alert on what changed while it was stopped
            self.tracker.next.pop(key, None)
            t["armed"] = True
        self.s.save()
        self.tracker.poke()

    def drop_track(self, svc: str, cc: str) -> None:
        key = tkey(svc, cc)
        for bucket in ("tracks", "last"):
            self.s.d[bucket].pop(key, None)
        self.tracker.next.pop(key, None)
        self.s.save()

    def n_on(self, svc: str | None = None) -> int:
        return sum(1 for t in self.s.d["tracks"].values() if t.get("on") and (svc is None or t["svc"] == svc))

    # -- screens
    def main_screen(self) -> Reply:
        s = self.s.d
        status = "⏸ paused" if s["paused"] else "▶️ running"
        mute = f" · 🔕 muted {int((s['mute_until'] - time.time()) / 60) + 1} min" if self.tracker.muted() else ""
        age = "never" if not s["last_poll"] else f"{int(time.time() - s['last_poll'])}s ago"
        txt = (f"<b>SmsCenter · price tracker</b>  {status}{mute}\n{self.n_on()} tracking now · {len(s['tracks'])} saved\n"
               f"Endpoints: {', '.join(ENDPOINTS[e] for e in self.s.endpoints)}\nDefault check every {secs(s['interval'])} · last poll {age}\n"
               f"API key: {'set ✅' if self.s.api_key else 'NOT SET ❗ /setkey'}")
        nw = sum(1 for w in s["watches"].values() if w.get("on"))
        nh = sum(1 for h in s["hunters"].values() if h.get("on"))
        txt += f"\n🎯 {nw} price watch{'es' if nw != 1 else ''} running · 🏹 {nh} hunter{'s' if nh != 1 else ''} hunting"
        txt += "\n<i>⏸ Pause only stops the price monitoring - hunters keep hunting.</i>"
        kb = [[("🏹 Hunter", "hu:l"), ("🎯 Price watch", "pw:l")],
              [("📧 Email", "em:m"), ("📱 Platforms", "pl:0")],
              [("📋 Tracked", "t:l")],
              [("⚙️ Settings", "s:m"), ("📜 History", "hi:0")],
              [("▶️ Resume monitoring" if s["paused"] else "⏸ Pause monitoring", "s:pause"), ("🔄 Check now", "k")]]
        return txt, kb

    async def platforms_screen(self, page: int) -> Reply:
        """Step 1: pick the platform."""
        svcs = await self.load_services()
        items = sorted(svcs.items(), key=lambda kv: (kv[0] != DEFAULT_SVC, kv[1].lower()))
        pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
        page = max(0, min(page, pages - 1))
        part = items[page * PER_PAGE:(page + 1) * PER_PAGE]
        kb: KB = [[("🔎 Search platform", "ps")]]
        for i in range(0, len(part), 2):
            kb.append([(f"{'✅ ' if self.n_on(code) else ''}{name[:20]}", f"pc:{code}:0") for code, name in part[i:i + 2]])
        nav = []
        if page > 0:
            nav.append(("⬅️", f"pl:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(("➡️", f"pl:{page + 1}"))
        kb.append(nav)
        kb.append([("📋 Tracked", "t:l"), ("🏠 Menu", "m")])
        return (f"📱 <b>Platforms</b> ({len(items)})\nStep 1: pick the platform, then the country.\n✅ = something is being tracked there "
                f"({self.n_on()} tracks running). Not in the list? Press 🔎 and type its code."), kb

    async def countries_screen(self, svc: str, page: int) -> Reply:
        """Step 2: pick the country for one platform."""
        if (r := self.need_key()):
            return r
        try:
            cs = await self.load_countries()
            sname = (await self.load_services()).get(svc, svc.upper())
        except SmsCenterError as e:
            return f"SmsCenter error: {html.escape(str(e))}", _nav(("⬅️ Platforms", "pl:0"))
        self.s.d["service"] = svc
        pages = max(1, (len(cs) + PER_PAGE - 1) // PER_PAGE)
        page = max(0, min(page, pages - 1))
        part = cs[page * PER_PAGE:(page + 1) * PER_PAGE]
        tracks = self.s.d["tracks"]
        kb: KB = [[("🔎 Search country", f"q:{svc}")]]
        for i in range(0, len(part), 2):
            kb.append([(f"{'✅ ' if tracks.get(tkey(svc, x['code']), {}).get('on') else ''}{flag(x['code'])} {x['name'][:18]}", f"v:{svc}:{x['code']}")
                       for x in part[i:i + 2]])
        nav = []
        if page > 0:
            nav.append(("⬅️", f"pc:{svc}:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(("➡️", f"pc:{svc}:{page + 1}"))
        kb.append(nav)
        kb.append([("▶️ Track ALL", f"all:{svc}"), ("⏹ Stop all", f"none:{svc}")])
        kb.append([("⬅️ Platforms", "pl:0"), ("🏠 Menu", "m")])
        return (f"🌍 <b>{html.escape(sname)}</b> · pick a country ({len(cs)})\nTap one to see its live prices and its control center.\n"
                f"✅ = tracking now ({self.n_on(svc)} for {html.escape(sname)})."), kb

    async def card(self, svc: str, cc: str) -> Reply:
        """Live prices from all three endpoints + the CONTROL CENTER (start/stop, interval, alerts) for this platform + country."""
        if (r := self.need_key()):
            return r
        key = tkey(svc, cc)
        try:
            sname = (await self.load_services()).get(svc, svc.upper())
            t = self.s.d["tracks"].get(key)
            name = t["name"] if t else next((x["name"] for x in await self.load_countries() if x["code"] == cc), cc.upper())
            cl = self.ui_client()
            try:
                raw = await fetch_country(cl, svc, cc, self.s.endpoints)
            finally:
                await cl.close()
        except SmsCenterError as e:
            return f"SmsCenter error: {html.escape(str(e))}", _nav((f"⬅️ Countries", f"pc:{svc}:0"))
        self.s.d["service"] = svc
        snap = snapshot(raw)
        lines = [f"{flag(cc)} <b>{html.escape(name)}</b> — {html.escape(sname)}"]
        if snap is None:
            lines += ["", f"{html.escape(sname)} is not offered by SmsCenter in this country at the moment."]
        else:
            o, l, n = raw.get("offer"), raw.get("list"), raw.get("count")
            api = snap["api"] or snap["list"] or snap["count"]
            tiers = list(o["tiers"]) if o and o["tiers"] else []
            if not tiers:                                                # offer endpoint off / empty: fall back to the single price the others give
                one = l and (l["price"], l["qty"]) or n and n["price"] and (n["price"], n["count"])
                tiers = [one] if one else []
            lines.append(f"<i>{qty(snap['stock'])} numbers · from {usd(cheapest(snap))}</i>")
            rows = []
            for price, stock in tiers[:20]:
                mark = "   ← API price" if api and abs(price - api) < 1e-9 else ""
                rows.append(f"{usd(price):>9}{qty(stock):>10}{mark}")
            if len(tiers) > 20:
                rows.append("     ...")
            lines += ["", pre(rows) if rows else "No price tiers returned."]
        on = bool(t and t.get("on"))
        iv = self.s.interval_of(t) if t else float(self.s.d["interval"])
        own = bool(t and t.get("interval"))
        lines.append("")
        lines.append(f"🎛 <b>Control center</b>: {'▶️ <b>TRACKING</b>' if on else '⏹ <b>not tracking</b>'} · checks every {secs(iv)}{'' if own else ' (default)'}")
        if t:
            u = t.get("under")
            lines.append(f"<i>🔔 any change {'on' if t.get('any') else 'off'} · drop {html.escape(drop_text(t, self.s.d['drop_pct']))} · limit {usd(u) if u else 'off'}{' · muted' if t.get('off') else ''}</i>")
        cur = cheapest(snap) if snap else None
        t = t or {}
        kb: KB = [[("⏹ Stop tracking" if on else "▶️ Start tracking", f"go:{svc}:{cc}"), ("🔄 Refresh", f"v:{svc}:{cc}")],
                  [(f"{'• ' if t.get('interval') == v else ''}{secs(v)}", f"iv:{svc}:{cc}:{v}") for v in INT_PRESETS],
                  [("⏱ Type seconds", f"w:int:{svc}:{cc}"), (f"{'• ' if not t.get('interval') else ''}default ({secs(self.s.d['interval'])})", f"iv:{svc}:{cc}:0")],
                  [(f"🔔 Any change: {'ON' if t.get('any') else 'off'}", f"a:{svc}:{cc}")],
                  [(f"{'• ' if (t.get('drop') or {}).get('pct') == p else ''}{p}%", f"dp:{svc}:{cc}:{p}") for p in (3, 5, 10, 20)] + [("off", f"dp:{svc}:{cc}:-1")],
                  [("📉 Set drop amount", f"w:drop:{svc}:{cc}"), ("🎯 Set price limit", f"w:under:{svc}:{cc}")]]
        if cur:
            kb.append([(f"🎯 −10% ({fmt(round(cur * 0.9, 4))})", f"u:{svc}:{cc}:10"), (f"🎯 −25% ({fmt(round(cur * 0.75, 4))})", f"u:{svc}:{cc}:25"),
                       ("limit off", f"u:{svc}:{cc}:0")])
        if t:
            kb.append([("🔔 Unmute" if t.get("off") else "🔕 Mute", f"n:{svc}:{cc}"), ("🗑 Remove", f"x:{svc}:{cc}")])
        kb.append([("⬅️ Countries", f"pc:{svc}:0"), ("🏠 Menu", "m")])
        return "\n".join(lines), kb

    def tracked_screen(self) -> Reply:
        tr = self.s.d["tracks"]
        if not tr:
            return "Nothing saved yet.", [[("📱 Platforms", "pl:0")], [("🏠 Menu", "m")]]
        items = sorted(tr.items(), key=lambda kv: (not kv[1].get("on"), kv[1]["sname"], kv[1]["name"]))
        kb = []
        for i in range(0, min(len(items), 40), 2):
            row = []
            for key, t in items[i:i + 2]:
                last = self.s.d["last"].get(key)
                price = f" {fmt(cheapest(last))}" if last and cheapest(last) else ""
                row.append((f"{'▶️' if t.get('on') else '⏹'} {t['sname'][:8]} {flag(t['cc'])} {t['name'][:10]}{price}{' 🔕' if t.get('off') else ''}",
                            f"v:{t['svc']}:{t['cc']}"))
            kb.append(row)
        kb.append([("📱 Add more", "pl:0"), ("🏠 Menu", "m")])
        return (f"📋 <b>Tracked</b> ({self.n_on()} running of {len(tr)}) - ▶️ running, ⏹ stopped. Price shown is the cheapest now.\n"
                "Tap one for its live price and control center."), kb

    def settings_screen(self) -> Reply:
        s = self.s.d

        def mark(cond, label):
            return ("✅ " if cond else "") + label
        kb = [[(mark(s["interval"] == v, secs(v)), f"s:i:{v}") for v in GLOBAL_PRESETS],
              [("⏱ Type default seconds", "s:w:gint")],
              [(mark(s["drop_pct"] == v, f"{v}%"), f"s:d:{v}") for v in (3, 5, 10, 20)],
              [(("✅ " if e in self.s.endpoints else "▫️ ") + ENDPOINTS[e], f"s:e:{e}") for e in ("offer", "list")],
              [(("✅ " if "count" in self.s.endpoints else "▫️ ") + ENDPOINTS["count"], "s:e:count")],
              [("🔕 30m", "s:mute:30"), ("🔕 2h", "s:mute:120"), ("🔔 unmute", "s:mute:0")],
              *([[("🔐 Checker session", "sess:go")]] if self.s.store.d["admin"] == self.uid else []),
              [("🏠 Menu", "m")]]
        return ("⚙️ <b>Settings</b>\nRow 1-2: DEFAULT check interval (a track can override it in its control center) · Row 3: default drop % · "
                "then which of the 3 endpoints to watch · mute everything.\n"
                f"Now: every {secs(s['interval'])} · default drop {s['drop_pct']:g}% · minimum interval {secs(MIN_INTERVAL)}\n"
                "<i>Short intervals on many tracks = many API calls per second.</i>"), kb

    # -- button presses
    async def ui(self, uid: int, data: str) -> Reply | None:
        self.pending.pop(uid, None)
        p = data.split(":")
        k, s = p[0], self.s
        if k == "m":
            return self.main_screen()
        if k == "noop":
            return None
        if k == "hu":
            return await self.ui_hunt(uid, p)
        if k == "em":
            return await self.ui_email(uid, p)
        if k == "rl":
            return await self.release(p[1])
        if k == "hi":
            return self.history_screen(int(p[1]))
        if k == "pw":
            return await self.ui_watch(uid, p)
        if k == "pl":
            return await self.platforms_screen(int(p[1]))
        if k == "ps":                                                    # 🔎 Search platform
            self.pending[uid] = ("psearch", "", "")
            return "🔎 <b>Search platform</b>\nType a platform name or its code (e.g. <code>whatsapp</code>, <code>wa</code>).", [[("⬅️ Platforms", "pl:0")]]
        if k == "pc":
            return await self.countries_screen(p[1], int(p[2]))
        if k == "q":                                                     # 🔎 Search country: the next text the owner types is the search
            self.pending[uid] = ("search", p[1], "")
            return "🔎 <b>Search country</b>\nType a country name, part of it, or its code (e.g. <code>ua</code>).", [[("⬅️ Countries", f"pc:{p[1]}:0")]]
        if k == "v":
            return await self.card(p[1], p[2])
        if k in ("go", "a", "dp", "n", "u", "iv", "x", "w"):
            return await self.track_action(uid, k, p)
        if k in ("all", "none", "allyes", "noneyes"):
            svc = p[1]
            sname = (await self.load_services()).get(svc, svc.upper())
            if k == "all":
                n = len(await self.load_countries())
                return (f"Track <b>{html.escape(sname)} in all {n} countries</b>?\nEvery check makes up to {n * len(s.endpoints)} API calls. "
                        "A longer interval is a good idea."), [[("✅ Yes, track all", f"allyes:{svc}"), ("Cancel", f"pc:{svc}:0")]]
            if k == "allyes":
                for c in await self.load_countries():
                    t = await self.ensure(svc, c["code"])
                    if not t["on"]:
                        t["on"] = True
                        s.d["last"].pop(tkey(svc, c["code"]), None)
                s.save()
                self.tracker.poke()
                return await self.countries_screen(svc, 0)
            if k == "none":
                return f"Stop tracking <b>{html.escape(sname)}</b> everywhere?", [[("✅ Yes, stop all", f"noneyes:{svc}"), ("Cancel", f"pc:{svc}:0")]]
            for t in s.d["tracks"].values():
                if t["svc"] == svc:
                    t["on"] = False
            s.save()
            return await self.countries_screen(svc, 0)
        if k == "t":
            return self.tracked_screen()
        if k == "k":
            if (r := self.need_key()):
                return r
            self.tracker.poke(force=True)
            return "🔄 Checking now - changes arrive as messages.", self.main_screen()[1]
        if k == "s":
            sub = p[1]
            if sub == "m":
                return self.settings_screen()
            if sub == "w":                                               # type the default interval
                self.pending[uid] = ("gint", "", "")
                return f"Send the default check interval in seconds (minimum {secs(MIN_INTERVAL)}), like <code>10</code>, <code>2.5</code> or <code>2m</code>.", [[("Cancel", "s:m")]]
            if sub == "i":
                s.d["interval"] = max(MIN_INTERVAL, float(p[2])); s.save(); self.tracker.poke()
            elif sub == "d":
                s.d["drop_pct"] = float(p[2]); s.save()
            elif sub == "e":
                cur = list(s.endpoints)
                cur = [x for x in cur if x != p[2]] if p[2] in cur else cur + [p[2]]
                s.d["endpoints"] = cur or ["offer"]
                s.d["last"].clear()                                      # prices from a different set of endpoints aren't comparable
                s.save()
            elif sub == "mute":
                m = int(p[2])
                s.d["mute_until"] = time.time() + m * 60 if m else 0.0
                s.save()
            elif sub == "pause":
                s.d["paused"] = not s.d["paused"]; s.save(); self.tracker.poke()
                return self.main_screen()
            return self.settings_screen()
        return self.main_screen()

    async def track_action(self, uid: int, k: str, p: list[str]) -> Reply | None:
        """Buttons of one platform+country card: start/stop, interval, alert settings."""
        s = self.s
        if k == "w":                                                     # w:<field>:<svc>:<cc> -> ask the owner to type a value
            field, svc, cc = p[1], p[2], p[3]
            t = await self.ensure(svc, cc)
            self.pending[uid] = (field, svc, cc)
            what = {"drop": "a drop amount like <code>0.05</code>, a percent like <code>10%</code>, or <code>off</code>",
                    "under": "the price at or below which you want an alert, like <code>1.20</code> (or <code>off</code>)",
                    "int": f"how often to check, in seconds (minimum {secs(MIN_INTERVAL)}): <code>1</code>, <code>7</code>, <code>2.5</code>, <code>2m</code> or <code>default</code>"}[field]
            return f"{flag(cc)} {html.escape(t['name'])} · {html.escape(t['sname'])}: send {what}.", [[("Cancel", f"v:{svc}:{cc}")]]
        svc, cc = p[1], p[2]
        t = await self.ensure(svc, cc)
        if k == "go":
            self.set_on(svc, cc, not t["on"])
        elif k == "a":
            t["any"] = not t.get("any")
        elif k == "dp":
            v = float(p[3])
            t["drop"] = "off" if v < 0 else ({"pct": v} if v else None)
        elif k == "n":
            t["off"] = not t.get("off")
        elif k == "iv":
            v = float(p[3])
            t["interval"] = max(MIN_INTERVAL, v) if v else None
            self.tracker.next.pop(tkey(svc, cc), None)
            self.tracker.poke()
        elif k == "u":
            pc = float(p[3])
            last = s.d["last"].get(tkey(svc, cc))
            cur = cheapest(last) if last else None
            if pc and cur:
                t["under"] = round(cur * (1 - pc / 100), 4)
            elif not pc:
                t["under"] = None
            t["armed"] = True
        elif k == "x":
            self.drop_track(svc, cc)
            return self.tracked_screen()
        s.save()
        return await self.card(svc, cc)

    # -- typed values (after pressing "Type seconds" / "Set drop amount" / "Set price limit")
    async def take_value(self, uid: int, text: str) -> Reply:
        field, svc, cc = self.pending.pop(uid)
        if field in ("emdomain", "emgmail", "empass"):
            return await self.take_email_value(field, text)
        if field in ("hcsearch", "hprefix", "hxprefix", "hmax", "hint", "hmin"):
            return await self.take_hunt_value(uid, field, svc, text)
        if field in ("wprice", "wlimit", "wint", "wstock"):
            return await self.take_watch_value(uid, field, svc, text)
        if field == "gint":
            v = parse_secs(text)
            if v is None or v < MIN_INTERVAL:
                self.pending[uid] = (field, svc, cc)
                return f"Send a number of seconds, at least {secs(MIN_INTERVAL)} (e.g. <code>10</code>).", [[("Cancel", "s:m")]]
            self.s.d["interval"] = v
            self.s.save()
            self.tracker.poke()
            return self.settings_screen()
        t = self.s.d["tracks"].get(tkey(svc, cc))
        if t is None:
            return "That track doesn't exist any more.", _nav()
        cancel = [[("Cancel", f"v:{svc}:{cc}")]]
        if field == "int":
            if text.strip().lower() in ("default", "reset", "off"):
                t["interval"] = None
            else:
                v = parse_secs(text)
                if v is None or v < MIN_INTERVAL:
                    self.pending[uid] = (field, svc, cc)
                    return f"Send a number of seconds, at least {secs(MIN_INTERVAL)} (e.g. <code>7</code>), or <code>default</code>.", cancel
                t["interval"] = v
            self.tracker.next.pop(tkey(svc, cc), None)
            self.tracker.poke()
        elif field == "drop":
            r = parse_drop(text)
            if not r:
                self.pending[uid] = (field, svc, cc)
                return "I didn't understand. Send <code>0.05</code> (amount), <code>10%</code>, <code>off</code> or <code>default</code>.", cancel
            kind, v = r
            t["drop"] = "off" if kind == "off" else (None if kind == "default" else {kind: v})
        else:
            tx = text.strip().lower().replace(",", ".")
            try:
                t["under"] = None if tx in ("off", "no", "none") else float(tx)
                if t["under"] is not None and t["under"] <= 0:
                    raise ValueError
            except ValueError:
                self.pending[uid] = (field, svc, cc)
                return "Send a price like <code>1.20</code>, or <code>off</code>.", cancel
            t["armed"] = True
        self.s.save()
        return await self.card(svc, cc)

    # -- hunter ---------------------------------------------------------------------------------
    def hunter_title(self, h: dict) -> str:
        return f"{html.escape(h['sname'])} · {flag(h['cc'])} {html.escape(h['name'])}"

    def hunters_screen(self) -> Reply:
        hs = self.s.d["hunters"]
        head = ("🏹 <b>Hunter</b>\nMake a hunter: platform, country, price tier, prefix, max price, how many numbers, and how often it asks.\n"
                "It buys numbers for you and tells you each one - with a Release button - and sends the code when it arrives.")
        kb = []
        for hid, h in sorted(hs.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
            kb.append([(f"{'▶️' if h.get('on') else '⏹'} #{hid} {h['sname'][:10]} {flag(h['cc'])} {h['name'][:12]} · {h['got']}/{h['want']}", f"hu:k:{hid}")])
        kb.append([("➕ Make hunter", "hu:n:0")])
        kb.append([("📜 History", "hi:0"), ("🏠 Menu", "m")])
        return head, kb

    async def hunter_platforms(self, page: int) -> Reply:
        svcs = await self.load_services()
        items = sorted(svcs.items(), key=lambda kv: (kv[0] != DEFAULT_SVC, kv[1].lower()))
        pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
        page = max(0, min(page, pages - 1))
        part = items[page * PER_PAGE:(page + 1) * PER_PAGE]
        kb: KB = []
        for i in range(0, len(part), 2):
            kb.append([(name[:20], f"hu:s:{code}") for code, name in part[i:i + 2]])
        nav = []
        if page > 0:
            nav.append(("⬅️", f"hu:n:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(("➡️", f"hu:n:{page + 1}"))
        kb.append(nav)
        kb.append([("⬅️ Hunters", "hu:l"), ("🏠 Menu", "m")])
        return "🏹 <b>Make hunter</b>\nStep 1: pick the platform. Step 2: country. Step 3: price tier.", kb

    async def hunter_countries(self, svc: str, page: int) -> Reply:
        if (r := self.need_key()):
            return r
        try:
            cs = await self.load_countries()
            sname = (await self.load_services()).get(svc, svc.upper())
        except SmsCenterError as e:
            return f"SmsCenter error: {html.escape(str(e))}", _nav()
        pages = max(1, (len(cs) + PER_PAGE - 1) // PER_PAGE)
        page = max(0, min(page, pages - 1))
        part = cs[page * PER_PAGE:(page + 1) * PER_PAGE]
        kb: KB = [[("🔎 Search country", f"hu:q:{svc}")]]
        for i in range(0, len(part), 2):
            kb.append([(f"{flag(x['code'])} {x['name'][:18]}", f"hu:c:{svc}:{x['code']}") for x in part[i:i + 2]])
        nav = []
        if page > 0:
            nav.append(("⬅️", f"hu:cp:{svc}:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(("➡️", f"hu:cp:{svc}:{page + 1}"))
        kb.append(nav)
        kb.append([("⬅️ Platforms", "hu:n:0"), ("🏠 Menu", "m")])
        return f"🏹 <b>{html.escape(sname)}</b> · step 2: pick the country ({len(cs)})", kb

    async def hunter_operators(self, hid: str) -> Reply:
        """Step 3: the price tiers (what the website shows). Tapping one sets the hunter's max price to that tier."""
        h = self.s.d["hunters"].get(hid)
        if h is None:
            return self.hunters_screen()
        o = None
        try:
            cl = self.ui_client()
            try:
                o = await cl.offer(h["svc"], h["cc"])
            finally:
                await cl.close()
        except SmsCenterError as e:
            return f"SmsCenter error: {html.escape(str(e))}", [[("⬅️ Back to the hunter", f"hu:k:{hid}")]]
        tiers = (o or {}).get("tiers") or []
        kb: KB = []
        for pr, q in tiers:
            kb.append([(f"{'✅ ' if h['maxp'] and abs(h['maxp'] - pr) < 1e-9 else ''}{usd(pr)} · {qty_short(q)} pc" + ("" if q else " (empty)"), f"hu:pr:{hid}:{pr:g}")])
        kb.append([("♾ No price limit", f"hu:pr:{hid}:0"), ("⬅️ Back to the hunter", f"hu:k:{hid}")])
        if not tiers:
            return (f"🏹 {self.hunter_title(h)}\nStep 3: SmsCenter has no price tiers for this right now.", kb)
        return (f"🏹 {self.hunter_title(h)}\nStep 3: pick the price tier you will pay up to.\n"
                f"<i>Each line = how many numbers cost that price or less (like the website).</i>", kb)

    async def live_prices(self, svc: str, cc: str) -> tuple[str, dict | None]:
        """The price of this platform in this country from all three endpoints."""
        try:
            cl = self.ui_client()
            try:
                raw = await fetch_country(cl, svc, cc, list(ENDPOINTS))             # a hunter always looks at all three endpoints
            finally:
                await cl.close()
        except SmsCenterError as e:
            return f"prices: {html.escape(str(e))}", None
        snap = snapshot(raw)
        if snap is None:
            return "Not offered by SmsCenter in this country right now.", None
        rows = [f"{'Cheapest tier':<16}{usd(snap['min']):>9}", f"{'API price':<16}{usd(snap['api']):>9}"]
        if snap.get("list") is not None and snap["list"] != snap["api"]:
            rows.append(f"{'List price':<16}{usd(snap['list']):>9}")
        if snap.get("count") is not None and snap["count"] != snap["api"]:
            rows.append(f"{'Count price':<16}{usd(snap['count']):>9}")
        rows.append(f"{'Stock':<16}{qty(snap['stock']):>9}")
        tiers = ((raw.get("offer") or {}).get("tiers")) or []
        if tiers:                                                        # what the website shows: "N numbers at this price or less"
            rows.append("Price tiers (numbers up to that price):")
            rows += [f"  {usd(pr):>9}  {qty(q):>9} pc" for pr, q in tiers]
        out = pre(rows)
        if snap.get("api") and tiers and not dict(tiers).get(snap["api"]) and snap.get("min") and snap["min"] > snap["api"]:
            out += (f"\n⚠️ The API price {usd(snap['api'])} has <b>0</b> numbers. Numbers only exist from {usd(snap['min'])} - "
                    f"set the hunter's Max price to at least {usd(snap['min'])} (button: = Cheapest).")
        return out, snap

    async def hunter_card(self, hid: str) -> Reply:
        h = self.s.d["hunters"].get(hid)
        if h is None:
            return self.hunters_screen()
        prices, snap = await self.live_prices(h["svc"], h["cc"]) if self.s.api_key else ("Set your SmsCenter key first: /setkey", None)
        pf = " ".join(filter(None, ["✅ " + ", ".join(h["prefix"]) if h["prefix"] else "", "🚫 " + ", ".join(h["xprefix"]) if h["xprefix"] else ""])) or "any number"
        state = "▶️ <b>hunting</b>" if h.get("on") else "⏹ stopped"
        txt = (f"🏹 <b>Hunter #{hid}</b> {state}\n{self.hunter_title(h)}\n"
               f"Bought <b>{h['got']}/{h['want']}</b> · requests {h['tries']} · released {h['rejected']} (prefix / price)\n"
               + ("🔔 stops and tells you if the price changes" if h["stopchg"] else "🔕 keeps hunting when the price changes (follows your max price)")
               + ("\n🛡 checks every number (" + self.checker.describe() + ") - banned / registered ones are cancelled and replaced" if h["check"] else "\n🛡 numbers are not checked")
               + (f"\n<i>{html.escape(h['last'])}</i>" if h.get("last") else "") + f"\n\n<b>Prices now</b>\n{prices}")
        rem = max(0, h["want"] - h["got"])
        until_t = h["until"] == "time"
        kb = [[(f"🔢 Prefix: {pf[:30]}", f"hu:w:prefix:{hid}")],
              [(f"💰 Max price: {usd(h['maxp']) if h['maxp'] else 'no limit'}", f"hu:w:max:{hid}")],
              [("➖", f"hu:m:{hid}"), (f"📦 Quantity: {rem} remaining (of {h['want']})", f"hu:k:{hid}"), ("➕", f"hu:a:{hid}")],
              [(f"⏱ Request every: {secs(h['interval'])}", f"hu:w:int:{hid}")],
              [(("✅ " if until_t else "") + f"⏰ Until time ({secs(h['minutes'] * 60)})", f"hu:u:time:{hid}"),
               (("" if until_t else "✅ ") + "🎯 Until quantity", f"hu:u:qty:{hid}")],
              [(("🔔 Stop if price changes: ON" if h["stopchg"] else "🔕 Stop if price changes: OFF"), f"hu:t:{hid}")],
              [(("🛡 Auto-check numbers: ON" if h["check"] else "🛡 Auto-check numbers: OFF"), f"hu:z:{hid}")],
              [("📊 Price tiers", f"hu:op:{hid}")],
              [("⏹ Stop hunting", f"hu:stop:{hid}") if h.get("on") else ("▶️ Start hunting", f"hu:go:{hid}"), ("🔄 Refresh", f"hu:k:{hid}")],
              [("🗑 Delete", f"hu:x:{hid}"), ("⬅️ Hunters", "hu:l"), ("🏠 Menu", "m")]]
        return txt, kb

    async def ui_hunt(self, uid: int, p: list[str]) -> Reply | None:
        sub, hs = p[1], self.s.d["hunters"]
        if sub == "l":
            return self.hunters_screen()
        if sub == "n":
            return await self.hunter_platforms(int(p[2]))
        if sub == "s":
            return await self.hunter_countries(p[2], 0)
        if sub == "cp":
            return await self.hunter_countries(p[2], int(p[3]))
        if sub == "q":
            self.pending[uid] = ("hcsearch", p[2], "")
            return "🔎 <b>Search country</b>\nType a country name or its code (e.g. <code>ua</code>).", [[("⬅️ Countries", f"hu:cp:{p[2]}:0")]]
        if sub == "c":                                                   # country chosen -> create the hunter, ask the operator
            if (r := self.need_key()):
                return r
            svc, cc = p[2], p[3]
            svcs, cs = await self.load_services(), await self.load_countries()
            hid = str(max([int(k) for k in hs if k.isdigit()] or [0]) + 1)
            hs[hid] = new_hunter(hid, svc, svcs.get(svc, svc.upper()), cc, next((c["name"] for c in cs if c["code"] == cc), cc.upper()))
            hs[hid]["interval"] = float(self.s.d.get("hunt_interval") or 1.0)
            self.s.save()
            return await self.hunter_operators(hid)
        hid = p[2] if sub in ("k", "op", "pr", "go", "stop", "x", "xy", "a", "m", "t", "px", "xx", "pm", "pc", "z") else (p[3] if len(p) > 3 else "")
        h = hs.get(hid)
        if h is None:
            return self.hunters_screen()
        if sub == "k":
            return await self.hunter_card(hid)
        if sub == "op":
            return await self.hunter_operators(hid)
        if sub == "pr":                                                  # hu:pr:<hid>:<price>  (0 = no limit)
            try:
                v = float(p[3])
            except ValueError:
                v = 0.0
            h["maxp"] = v if v > 0 else None
            self.s.save()
            return await self.hunter_card(hid)
        if sub == "w":                                                   # hu:w:<field>:<hid> -> type a value
            field = p[2]
            if field == "prefix":
                return self.prefix_screen(uid, hid)
            self.pending[uid] = ({"prefix": "hprefix", "max": "hmax", "int": "hint", "min": "hmin"}[field], hid, "")
            what = {"prefix": ("the prefix(es) the number must start with, separated by spaces or commas.\n<code>+38067</code> = the whole number starts with it · "
                               "<code>67</code> or <code>067</code> = after the country code. <code>off</code> = any number"),
                    "max": "the max price per number, like <code>0.40</code> - or <code>cheapest</code> / <code>api</code> to use the current price, or <code>off</code>",
                                        "int": f"how often to send a request, in seconds (minimum {secs(HUNT_MIN)}): <code>0.1</code>, <code>1</code>, <code>2.5</code>",
                    "min": "stop hunting after how many minutes, like <code>20</code>"}[field]
            return f"🏹 {self.hunter_title(h)}: send {what}.", [[("Cancel", f"hu:k:{hid}")] + ([("📊 Price tiers", f"hu:op:{hid}")] if field == "max" else [])]
        if sub in ("px", "xx"):                                          # hu:px:<hid>:<i> remove one wanted prefix · hu:xx:<hid>:<i> remove one blocked prefix
            lst, i = h["prefix" if sub == "px" else "xprefix"], int(p[3])
            if 0 <= i < len(lst):
                lst.pop(i)
                self.s.save()
            return self.prefix_screen(uid, hid, "in" if sub == "px" else "ex")
        if sub == "pm":                                                  # hu:pm:<hid>:<in|ex>  which list the next typed prefix goes to
            return self.prefix_screen(uid, hid, p[3])
        if sub == "pc":                                                  # hu:pc:<hid>:<in|ex>  clear that list
            h["prefix" if p[3] == "in" else "xprefix"] = []
            self.s.save()
            return self.prefix_screen(uid, hid, p[3])
        if sub in ("a", "m"):                                            # quantity: one more / one less remaining
            h["want"] = max(h["got"] + 1, h["want"] + (1 if sub == "a" else -1))
            self.s.save()
            return await self.hunter_card(hid)
        if sub == "z":                                                   # auto-check on / off
            h["check"] = not h["check"]
            self.s.save()
            return await self.hunter_card(hid)
        if sub == "t":                                                   # stop-if-the-price-changes on / off
            h["stopchg"] = not h["stopchg"]
            self.s.save()
            return await self.hunter_card(hid)
        if sub == "u":                                                   # hu:u:<time|qty>:<hid>
            if p[2] == "qty":
                h["until"] = "qty"
                self.s.save()
                return await self.hunter_card(hid)
            h["until"] = "time"
            self.pending[uid] = ("hmin", hid, "")
            self.s.save()
            return f"🏹 {self.hunter_title(h)}: send how many <b>minutes</b> to hunt, like <code>20</code> (now {secs(h['minutes'] * 60)}).", [[("Keep it", f"hu:k:{hid}")]]
        if sub == "go":
            return await self.start_hunter(hid)
        if sub == "stop":
            await self.stop_hunter(hid, "stopped by you")
            return await self.hunter_card(hid)
        if sub == "x":
            return f"Delete hunter <b>#{hid}</b> ({self.hunter_title(h)})?", [[("✅ Yes, delete", f"hu:xy:{hid}"), ("Cancel", f"hu:k:{hid}")]]
        if sub == "xy":
            await self.stop_hunter(hid, "deleted", say=False)
            hs.pop(hid, None)
            self.s.save()
            return self.hunters_screen()
        return self.hunters_screen()

    def prefix_screen(self, uid: int, hid: str, mode: str = "in") -> Reply:
        """Two lists: ✅ 'only these' (empty = any) and 🚫 'never these'. The next prefix you type is added to the list in `mode`."""
        h = self.s.d["hunters"][hid]
        ex = mode == "ex"
        self.pending[uid] = ("hxprefix" if ex else "hprefix", hid, "")
        inc = ", ".join(h["prefix"]) if h["prefix"] else "any"
        exc = ", ".join(h["xprefix"]) if h["xprefix"] else "none"
        txt = (f"🏹 {self.hunter_title(h)}\n🔢 <b>Prefixes</b>\n✅ Only these: <b>{html.escape(inc)}</b>\n🚫 Never these: <b>{html.escape(exc)}</b>\n\n"
               f"Now adding to the <b>{'🚫 NEVER' if ex else '✅ ONLY'}</b> list - send a prefix (one, or several separated by spaces).\n"
               "<code>+38067</code> = the whole number starts with it · <code>67</code> or <code>067</code> = after the country code.\n"
               "A number is kept when it matches an ✅ prefix (if you set any) and matches no 🚫 prefix. Tap a prefix below to remove it.")
        kb: KB = [[(("✅ " if not ex else "") + "➕ Add to ONLY list", f"hu:pm:{hid}:in"), (("✅ " if ex else "") + "🚫 Add to NEVER list", f"hu:pm:{hid}:ex")]]
        kb += [[(f"❌ ✅ {x}", f"hu:px:{hid}:{i}")] for i, x in enumerate(h["prefix"])]
        kb += [[(f"❌ 🚫 {x}", f"hu:xx:{hid}:{i}")] for i, x in enumerate(h["xprefix"])]
        if h["xprefix"] if ex else h["prefix"]:
            kb.append([(f"🧹 Clear {'🚫 never' if ex else '✅ only'} list", f"hu:pc:{hid}:{'ex' if ex else 'in'}")])
        kb.append([("✅ Done", f"hu:k:{hid}")])
        return txt, kb

    async def take_hunt_value(self, uid: int, field: str, a: str, text: str) -> Reply:
        if field == "hcsearch":                                          # a = platform code
            t = text.strip().lower()
            hits = [x for x in await self.load_countries() if t in x["name"].lower() or t == x["code"]][:12]
            if not hits:
                self.pending[uid] = ("hcsearch", a, "")
                return f"No country matches “{html.escape(text.strip())}”.", [[("⬅️ Countries", f"hu:cp:{a}:0")]]
            if len(hits) == 1:
                return await self.ui_hunt(uid, ["hu", "c", a, hits[0]["code"]])
            kb = [[(f"{flag(x['code'])} {x['name'][:18]}", f"hu:c:{a}:{x['code']}") for x in hits[i:i + 2]] for i in range(0, len(hits), 2)]
            kb.append([("⬅️ Countries", f"hu:cp:{a}:0")])
            return f"🔎 {len(hits)} matches - tap one", kb
        h = self.s.d["hunters"].get(a)
        if h is None:
            return self.hunters_screen()
        back = [[("Cancel", f"hu:k:{a}")]]
        t = text.strip().lower().replace(",", ".").lstrip("$")
        if field in ("hprefix", "hxprefix"):
            key = "prefix" if field == "hprefix" else "xprefix"
            if t in ("off", "none", "clear", "any", "all", "no"):
                h[key] = []
            else:
                add = parse_prefixes(text)
                if not add:
                    self.pending[uid] = (field, a, "")
                    return "Send a prefix like <code>+38067</code> or <code>67</code> (several: separate with spaces).", back
                h[key] = [*h[key], *[x for x in add if x not in h[key]]]                  # typing ADDS to the list
        elif field == "hmax":
            if t in ("cheapest", "min", "api"):
                _txt, snap = await self.live_prices(h["svc"], h["cc"])
                price = (snap or {}).get("api" if t == "api" else "min") or (snap or {}).get("api")
                if not price:
                    self.pending[uid] = (field, a, "")
                    return "Couldn't read the price right now - send a number like <code>0.40</code>.", back
                h["maxp"] = float(price)
                self.s.save()
                return await self.hunter_card(a)
            try:
                h["maxp"] = None if t in ("off", "no", "none", "0") else float(t)
                if h["maxp"] is not None and h["maxp"] <= 0:
                    raise ValueError
            except ValueError:
                self.pending[uid] = (field, a, "")
                return "Send a price like <code>0.40</code>, or <code>cheapest</code>, or <code>off</code>.", back
        elif field == "hint":
            v = parse_secs(text)
            if v is None or v < HUNT_MIN:
                self.pending[uid] = (field, a, "")
                return f"Send a number of seconds, at least {secs(HUNT_MIN)} (e.g. <code>0.1</code> or <code>1</code>).", back
            h["interval"] = v
            self.s.d["hunt_interval"] = v
        else:
            v = parse_secs(text + "m") if t.replace(".", "", 1).isdigit() else parse_secs(text)
            if v is None or v < 60:
                self.pending[uid] = (field, a, "")
                return "Send the number of minutes, like <code>20</code>.", back
            h["minutes"] = v / 60
        self.s.save()
        if field in ("hprefix", "hxprefix"):                              # stay on the prefix list so more can be added
            return self.prefix_screen(uid, a, "ex" if field == "hxprefix" else "in")
        return await self.hunter_card(a)

    async def start_hunter(self, hid: str) -> Reply:
        h = self.s.d["hunters"][hid]
        if (r := self.need_key()):
            return r
        if hid in self.hunts and not self.hunts[hid].done():
            return await self.hunter_card(hid)
        if sum(1 for t in self.hunts.values() if not t.done()) >= MAX_HUNTERS_ON:
            return f"At most {MAX_HUNTERS_ON} hunters can hunt at the same time. Stop one first.", [[("⬅️ Hunters", "hu:l")]]
        if h["got"] >= h["want"]:                                        # the last batch is complete: this is a fresh one
            h["got"] = 0
        h.update(on=True, tries=0, rejected=0, last="")
        self.s.save()
        self.hunts[hid] = asyncio.create_task(self.run_hunter(hid))
        return await self.hunter_card(hid)

    async def stop_hunter(self, hid: str, why: str, say: bool = True) -> None:
        h = self.s.d["hunters"].get(hid)
        t = self.hunts.pop(hid, None)
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):                  # noqa: BLE001
                pass
        if h is not None:
            was_on = h.get("on")
            h["on"], h["last"] = False, why
            self.s.save()
            if say and was_on:
                await self.notify(f"⏹ <b>Hunter #{hid}</b> stopped: {html.escape(why)}\nBought {h['got']}/{h['want']} · {h['tries']} requests.")

    async def run_hunter(self, hid: str) -> None:
        """The hunt: ask SmsCenter for a number every `interval` seconds. A number that doesn't match the prefix (or is over the max price)
        is released straight away; a matching one is kept and announced. Ends when it has `want` matching numbers, the time is up, or a real
        problem (no balance, bad key...) appears."""
        h = self.s.d["hunters"][hid]
        client = self.client_factory()
        end = time.time() + h["minutes"] * 60
        why, errs, stuck = "", 0, 0
        h["stuck_n"] = 0
        base, next_check = None, 0.0
        broke_since, told_wait = 0.0, False
        try:
            while h["got"] < h["want"]:
                if h["until"] == "time" and time.time() > end:
                    why = f"time is up ({secs(h['minutes'] * 60)})"
                    break
                t0 = time.time()
                if h.get("stuck_n", 0) >= 3:
                    why = "3 numbers could not be cancelled - stopped to protect your balance"
                    break
                if h["stopchg"] and t0 >= next_check:                    # watch the price: stop and tell you the moment it moves
                    next_check = t0 + PRICE_CHECK
                    try:
                        o = await client.offer(h["svc"], h["cc"])
                        sig = (o.get("min"), o.get("api")) if o else None
                    except SmsCenterError:
                        sig = None
                    if sig is not None:
                        if base is None:
                            base = sig
                        elif sig != base:
                            why = f"the price changed: cheapest {usd(base[0])} → {usd(sig[0])} · API price {usd(base[1])} → {usd(sig[1])}"
                            break
                try:
                    num = await client.get_number(h["svc"], h["cc"], h["op"], h["maxp"])
                    errs, broke_since, told_wait = 0, 0.0, False
                    h["last"] = ""
                except SmsCenterError as e:
                    code = str(e)
                    if code == "noMoney":
                        # Not fatal: numbers we released are refunded a moment later, so keep asking for a while - the hunt continues the
                        # second the balance is back. Only a balance that STAYS too low ends it.
                        broke_since = broke_since or time.time()
                        h["last"] = "waiting for my balance to come back (refund of released numbers)…"
                        if time.time() - broke_since < NOMONEY_WAIT:
                            if not told_wait:
                                told_wait = True
                                await self.notify(f"⏳ <b>Hunter #{hid}</b>: SmsCenter says the balance is too low right now - waiting for the refund of the released numbers, "
                                                  f"then I carry on (up to {secs(NOMONEY_WAIT)}).")
                            await asyncio.sleep(max(1.0, h["interval"]))
                            continue
                        try:
                            bal = await client.balance()
                        except SmsCenterError:
                            bal = None
                        why = f"balance too low for {secs(NOMONEY_WAIT)}" + (f" (balance {usd(bal)})" if bal is not None else "") + " - deposit and start again"
                        break
                    h["tries"] += 1
                    if code in HUNT_FATAL:
                        why = HUNT_FATAL[code]
                        break
                    if code != "noNumber":                               # noNumber = nothing in stock right now: just keep asking
                        errs += 1
                        if errs >= 8:
                            why = f"SmsCenter keeps answering “{code}”"
                            break
                    await asyncio.sleep(max(0.0, h["interval"] - (time.time() - t0)))
                    continue
                h["tries"] += 1
                too_dear = bool(h["maxp"] and num["price"] and num["price"] > h["maxp"] + 1e-9)
                if not too_dear and number_ok(num["tel"], h["prefix"], h["xprefix"]):
                    entry = self.record_purchase(h, num)
                    entry["chk"] = "pending" if h["check"] else "off"
                    h["got"] += 1                                        # provisional: a banned / registered result gives the place back
                    self.s.save()
                    entry["mid"] = await self.notify_kb(*self.purchase_card(entry))
                    self.s.save()
                    self.code_tasks[entry["idNum"]] = asyncio.create_task(self.wait_code(entry["idNum"]))      # the code is watched from the first second
                    if h["check"]:                                       # ... and the check runs beside the hunt, it never blocks it
                        t = asyncio.create_task(self.check_purchase(h, entry))
                        self.check_tasks.setdefault(hid, set()).add(t)
                        t.add_done_callback(lambda t_, hid=hid: self.check_tasks.get(hid, set()).discard(t_))
                    else:
                        await self.settle_good(entry)
                else:
                    h["rejected"] += 1
                    if not await self.release_quiet(client, num["idNum"]):
                        stuck += 1
                        self.record_purchase(h, num, state="kept")      # it could not be released: it is yours (and paid) - it shows in History
                        if stuck >= 3:
                            why = "3 numbers that don't match could not be released - stopped to protect your balance"
                            break
                await asyncio.sleep(max(0.0, h["interval"] - (time.time() - t0)))
            else:
                why = ""
        except asyncio.CancelledError:
            raise
        except Exception as e:                                           # noqa: BLE001
            log.exception("hunter %s crashed", hid)
            why = f"error: {type(e).__name__}"
        finally:
            try:
                await client.close()
            except Exception:                                            # noqa: BLE001
                pass
        tasks = [t for t in self.check_tasks.get(hid, ()) if not t.done()]
        if not why and tasks and h["got"] >= h["want"]:                 # got the amount, but some numbers are still being checked
            h["last"] = "waiting for the number checks…"
            self.s.save()
            await asyncio.gather(*tasks, return_exceptions=True)
            if h["got"] < h["want"] and h.get("stuck_n", 0) < 3:        # a bad number gave its place back: hunt on
                self.hunts[hid] = asyncio.create_task(self.run_hunter(hid))
                return
        h["on"], h["last"] = False, why or "done"
        self.hunts.pop(hid, None)
        self.s.save()
        if h["got"] >= h["want"]:
            await self.notify(f"🏁 <b>Hunter #{hid}</b> finished: bought <b>{h['got']}/{h['want']}</b> {html.escape(h['sname'])} numbers ({flag(h['cc'])} {html.escape(h['name'])}).\n"
                              f"{h['tries']} requests · {h['rejected']} numbers released (prefix / price).")
        else:
            await self.notify(f"⏹ <b>Hunter #{hid}</b> stopped: {html.escape(why)}\nBought {h['got']}/{h['want']} · {h['tries']} requests · {h['rejected']} released.")

    async def release_quiet(self, client, id_num: str) -> bool:
        for _ in range(3):
            try:
                if await client.set_status(id_num, "end") in ("ready", "update"):
                    return True
                return False                                             # smsReceived / waitSms: too late
            except SmsCenterError:
                await asyncio.sleep(0.2)
        return False

    # -- bought numbers: history, code, release
    def record_purchase(self, h: dict, num: dict, state: str = "active") -> dict:
        e = {"t": time.time(), "hid": h["id"], "svc": h["svc"], "sname": h["sname"], "cc": h["cc"], "name": h["name"], "op": h["op"] or "any",
             "tel": num["tel"], "idNum": num["idNum"], "price": num["price"], "code": None, "state": state}
        hist = self.s.d["history"]
        hist.insert(0, e)
        del hist[HIST_KEEP:]
        return e

    def find_purchase(self, id_num: str) -> dict | None:
        return next((e for e in self.s.d["history"] if e["idNum"] == id_num), None)

    async def settle_good(self, e: dict) -> None:
        """A number that is clean (or couldn't be checked, or checking is off): refresh its card and pin it."""
        await self.refresh_card(e)
        if e.get("mid") and not e.get("pinned") and e["state"] in ("active", "code"):
            e["pinned"] = True
            self.s.save()
            await self.pin_msg(e["mid"])

    async def check_purchase(self, h: dict, e: dict) -> None:
        """Ask the checker bot about one bought number. Banned / registered -> cancel it (balance back) and the hunter buys another."""
        try:
            try:
                e["chk"], e["chk_note"] = await asyncio.wait_for(self.checker.check(e["tel"]), CHECK_TIMEOUT)
            except asyncio.TimeoutError:
                e["chk"], e["chk_note"] = "unknown", "the checker bot didn't answer in time"
            if e["chk"] in ("banned", "registered") and e["state"] == "active":
                e["releasing"] = True
                client = self.client_factory()
                try:
                    ok = await self.release_quiet(client, e["idNum"])
                finally:
                    await client.close()
                    e.pop("releasing", None)
                h["got"] = max(0, h["got"] - 1)
                h["rejected"] += 1
                if ok:
                    e["state"], e["auto"] = "released", True
                    t = self.code_tasks.pop(e["idNum"], None)
                    if t:
                        t.cancel()
                else:
                    e["state"] = "kept"
                    h["stuck_n"] = h.get("stuck_n", 0) + 1
                self.s.save()
                await self.refresh_card(e)
            else:
                self.s.save()
                await self.settle_good(e)
            await self.refresh_code_msg(e)
        except asyncio.CancelledError:
            if e.get("chk") == "pending":
                e["chk"] = "unknown"
                self.s.save()
            raise
        except Exception:                                                # noqa: BLE001
            log.exception("check of %s failed", e.get("tel"))
            e["chk"], e["chk_note"] = "unknown", "check failed"
            self.s.save()

    def code_card(self, e: dict) -> Reply:
        chk = {"clean": "🟢 Clean", "banned": "🔴 Banned", "registered": "🟠 Registered", "pending": "🔎 Checking…"}.get(e.get("chk", "off"), "⚪ Not checked")
        return (f"📩 <b>Code</b>  <code>{html.escape(e['code'] or '')}</code>\n📞 <code>+{html.escape(e['tel'])}</code>\n"
                f"{chk} · {flag(e['cc'])} {html.escape(e['name'])} · {html.escape(e['sname'])}",
                [[("📋 Copy code", f"copy:{e['code']}"), ("📋 Copy number", f"copy:+{e['tel']}")]])

    async def refresh_code_msg(self, e: dict) -> None:
        if e.get("cmid") and e.get("code"):
            await self.edit_msg(e["cmid"], *self.code_card(e))

    CHK_LINE = {"pending": "🔎 Checking the number…", "clean": "🟢 <b>Clean</b> - not registered, not banned",
                "banned": "🔴 <b>Banned</b> on Telegram", "registered": "🟠 <b>Already registered</b> on Telegram", "off": "⚪ Not checked"}

    def purchase_card(self, e: dict) -> Reply:
        """One card per bought number - the same renderer for the first message, every edit, the Release button and the History."""
        chk, st = e.get("chk", "off"), e["state"]
        note = (e.get("chk_note") or "").strip()
        if chk == "unknown":
            chk_line = "⚪ Couldn't check it" + (f" ({html.escape(note)})" if note else "")
        elif chk == "off" and note:
            chk_line = f"⚪ Not checked ({html.escape(note)})"
        else:
            chk_line = self.CHK_LINE.get(chk, "⚪ Not checked")
        if st == "released":
            head = f"❌ <b>Rejected</b> - cancelled, balance back" if e.get("auto") else "🔓 <b>Cancelled</b> - balance back"
        elif st == "kept":
            head = "⚠️ <b>Could not be cancelled</b> - it is yours"
        elif st == "cancelled":
            head = "🚫 <b>Ended on SmsCenter</b>"
        elif chk in ("banned", "registered"):
            head = "❌ <b>Rejected</b>"
        elif st == "code":
            head = "📩 <b>Code received</b>"
        elif chk == "pending":
            head = "🛒 <b>Number bought</b>"
        else:
            head = "✅ <b>Number ready</b>"
        lines = [head, f"📞 <code>+{html.escape(e['tel'])}</code>",
                 f"{flag(e['cc'])} {html.escape(e['name'])} · {html.escape(e['sname'])}" + (f" · {usd(e['price'])}" if e.get("price") else "")
                 + (f" · hunter #{e['hid']}" if e.get("hid") else ""), chk_line]
        if st == "active":
            lines.append("⏳ Waiting for the code…" if chk not in ("banned", "registered") else "")
        elif st == "code":
            lines.append(f"📩 Code: <code>{html.escape(e['code'] or '')}</code>")
        elif st == "expired":
            lines.append("⌛ No code arrived - expired")
        elif st == "cancelled":
            lines.append(f"🚫 SmsCenter ended it after {mmss(e.get('ended_after') or 0)} - it <b>timed out</b>, or it was <b>cancelled from the panel</b>. No code.")
        kb: KB = [[("📋 Copy number", f"copy:+{e['tel']}")]]
        if st == "code" and e.get("code"):
            kb[0].append(("📋 Copy code", f"copy:{e['code']}"))
        if st == "active":
            kb.append([("🔓 Release (cancel + balance back)", f"rl:{e['idNum']}")])
        return "\n".join(x for x in lines if x), kb

    async def refresh_card(self, e: dict) -> None:
        await self.edit_msg(e.get("mid"), *self.purchase_card(e))

    async def wait_code(self, id_num: str) -> None:
        """Watch one bought number until its code arrives (-> a reply under the number's message), or SmsCenter ends it (timed out / cancelled in
        the panel -> the card says so and is unpinned). Uses the v0 getStatus, which is the only call that tells 'cancelled' from 'waiting'."""
        e = self.find_purchase(id_num)
        client = self.client_factory()
        end = max((e["t"] if e else time.time()) + CODE_WAIT, time.time() + 30)
        try:
            while e and e["state"] == "active" and time.time() < end:
                try:
                    st, code = await client.v0_status(id_num)
                except SmsCenterError:
                    st, code = "?", None
                if st == "?":                                            # v0 didn't answer: fall back to the v1 code call
                    try:
                        code = await client.sms_code(id_num)
                    except SmsCenterError:
                        code = None
                    st = "ok" if code else "wait"
                if st == "ok" and code:
                    e["code"], e["state"] = code, "code"
                    self.s.save()
                    await self.refresh_card(e)                           # the number's own message shows the code too
                    e["cmid"] = await self.notify_kb(*self.code_card(e), reply_to=e.get("mid"))
                    self.s.save()
                    return
                if st == "cancel" and e["state"] == "active" and not e.get("releasing"):
                    e["state"], e["ended_after"] = "cancelled", time.time() - e["t"]
                    self.s.save()
                    if e.get("pinned"):
                        e["pinned"] = False
                        await self.unpin_msg(e.get("mid"))
                    await self.refresh_card(e)
                    await self.notify_kb(f"🚫 <b>+{html.escape(e['tel'])}</b> ended on SmsCenter after {mmss(e['ended_after'])} - it timed out or was cancelled from the panel.",
                                         None, reply_to=e.get("mid"))
                    return
                await asyncio.sleep(CODE_POLL)
            if e and e["state"] == "active":
                e["state"] = "expired"
                self.s.save()
                await self.refresh_card(e)
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await client.close()
            except Exception:                                            # noqa: BLE001
                pass
            self.code_tasks.pop(id_num, None)

    # -- catch-all e-mail: your domain forwards everything to your Gmail; the bot makes believable addresses and hands you the codes that arrive
    def mail_cfg(self) -> tuple[str, str, str] | None:
        return self.s.store.mail_cfg()

    def is_admin(self) -> bool:
        return self.s.store.d["admin"] == self.uid

    def start_mail(self) -> None:
        if self.hub:
            self.hub.start_mail()

    def mail_rec(self, key: str, by: str = "id") -> dict | None:
        return next((r for r in self.s.mail["generated"] if r.get(by) == key), None)

    async def mail_notify(self, it: dict) -> None:
        rec = self.mail_rec(it["to"], "addr")
        body = it["body"][:300] + ("…" if len(it["body"]) > 300 else "")
        txt = (f"📧 <b>New email</b> → <code>{html.escape(it['to'])}</code>\n"
               f"From: {html.escape(it['from'][:80])}\nSubject: {html.escape(it['subject'][:120])}\n")
        if it.get("code"):
            txt += f"\n🔑 <b>Code:</b> <code>{it['code']}</code>\n"
        if body:
            txt += f"\n<i>{html.escape(body)}</i>"
        kb: KB = []
        if it.get("code"):
            kb.append([(f"📋 Copy code {it['code']}", f"copy:{it['code']}")])
        kb.append([("📋 Copy email", f"copy:{it['to']}")])
        kb.append([("📥 Inbox", f"em:i:{rec['id']}")] if rec else [("📧 Email", "em:m")])
        await self.notify_kb(txt, kb)

    def email_menu(self, note: str = "") -> Reply:
        m = self.s.mail
        ok = lambda v: f"<code>{html.escape(v)}</code> ✅" if v else "not set ❗"                      # noqa: E731
        txt = "📧 <b>Catch-all email</b>\n"
        if self.is_admin():
            txt += (f"🌐 Domain: {ok(m['domain'])}\n📮 Gmail: {ok(m['gmail'])}\n🔑 App password: {'set ✅' if m['app_password'] else 'not set ❗'}\n\n"
                    "Point your domain's <b>catch-all</b> at that Gmail. Then <b>Generate</b> gives everyone a real-looking address on your domain - ")
        else:
            txt += "<b>Generate</b> gives you a real-looking email address - "
        txt += "when a code or mail arrives for it, I send it here with a copy button."
        if note:
            txt = note + "\n\n" + txt
        kb: KB = []
        if self.mail_cfg():
            kb.append([("✉️ Generate email", "em:g"), ("📋 My emails", "em:l")])
        if self.is_admin():
            kb += [[("🌐 Domain", "em:s:d"), ("📮 Gmail", "em:s:g")], [("🔑 App password", "em:s:p")]]
        return txt, kb + [[("🏠 Menu", "m")]]

    def mail_unset(self) -> Reply:
        return self.email_menu("Set the domain, Gmail and App password first." if self.is_admin() else "The admin hasn't set up email yet.")

    def email_card(self, rec: dict, note: str = "") -> Reply:
        txt = f"✉️ <b>{html.escape(note or 'Your email')}</b>\n<code>{html.escape(rec['addr'])}</code>\n👤 {html.escape(rec['name'])}\n\n<i>Codes and mails sent to it show up here by themselves.</i>"
        return txt, [[("📋 Copy email", f"copy:{rec['addr']}")], [("📋 Copy name", f"copy:{rec['name']}")],
                     [("📥 Inbox", f"em:i:{rec['id']}"), ("🔁 Another", "em:g")], [("📧 Email", "em:m"), ("🏠 Menu", "m")]]

    async def ui_email(self, uid: int, p: list[str]) -> Reply:
        m, a = self.s.mail, (p[1] if len(p) > 1 else "m")
        if a == "m":
            return self.email_menu()
        if a == "s":
            if not self.is_admin():
                self.pending.pop(uid, None)
                return self.email_menu("Only the admin can change this.")
            what = p[2] if len(p) > 2 else "d"
            field, prompt = {"d": ("emdomain", "🌐 Send your <b>domain</b> (e.g. <code>mydomain.com</code>)."),
                             "g": ("emgmail", "📮 Send the <b>Gmail address</b> your catch-all forwards to."),
                             "p": ("empass", "🔑 Send the Gmail <b>App password</b> (16 letters, Google account → Security → 2-Step Verification → App passwords). I delete your message right after.")}[what]
            self.pending[uid] = (field, "", "")
            return prompt, [[("Cancel", "em:m")]]
        if a == "g":
            cfg = self.mail_cfg()
            if not cfg:
                return self.mail_unset()
            addr, name = catchall.generate(cfg[2], set(self.s.store.mail_owners()))
            m["n"] += 1
            rec = {"id": str(m["n"]), "addr": addr, "name": name, "ts": time.time()}
            m["generated"] = (m["generated"] + [rec])[-60:]
            self.s.save()
            self.start_mail()
            return self.email_card(rec, "New email")
        if a == "l":
            recs = m["generated"][-10:][::-1]
            if not recs:
                return "No emails generated yet.", [[("✉️ Generate email", "em:g")], [("📧 Email", "em:m")]]
            return "📋 <b>Your emails</b> (latest first)", [[(r["addr"], f"em:v:{r['id']}")] for r in recs] + [[("📧 Email", "em:m")]]
        rec = self.mail_rec(p[2]) if len(p) > 2 else None
        if rec is None:
            return self.email_menu("I don't know that email any more.")
        if a == "v":
            return self.email_card(rec)
        if a == "i":
            cfg = self.mail_cfg()
            if not cfg:
                return self.mail_unset()
            try:
                mails = await catchall.inbox(cfg[0], cfg[1], rec["addr"])
            except Exception as e:                                       # noqa: BLE001
                return f"Could not read the Gmail: {html.escape(type(e).__name__)} {html.escape(str(e)[:100])}", [[("🔄 Retry", f"em:i:{rec['id']}")], [("📧 Email", "em:m")]]
            txt = f"📥 <b>Inbox</b> · <code>{html.escape(rec['addr'])}</code>\n"
            kb: KB = []
            if not mails:
                txt += "\nNothing has arrived yet."
            for x in mails:
                when = time.strftime("%H:%M", time.localtime(x["ts"])) if x["ts"] else ""
                txt += f"\n<b>{html.escape(x['subject'][:100] or '(no subject)')}</b> {when}{' · spam' if x['spam'] else ''}\n{html.escape(x['from'][:70])}\n"
                if x["code"]:
                    txt += f"🔑 <code>{x['code']}</code>\n"
                    kb.append([(f"📋 Copy code {x['code']}", f"copy:{x['code']}")])
            return txt, kb + [[("🔄 Refresh", f"em:i:{rec['id']}"), ("⬅️ Back", f"em:v:{rec['id']}")]]
        return self.email_menu()

    async def take_email_value(self, field: str, text: str) -> Reply:
        m, v = self.s.mail, text.strip()
        if not self.is_admin():
            self.pending.pop(self.uid, None)
            return self.email_menu("Only the admin can change this.")
        if field == "emdomain":
            v = re.sub(r"^(https?://)?(www\.)?|^@|/.*$", "", v.lower())
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", v):
                self.pending[self.uid] = (field, "", "")
                return "That doesn't look like a domain. Send it like <code>mydomain.com</code>.", [[("Cancel", "em:m")]]
            m["domain"] = v
            self.s.save()
            return self.email_menu("✅ Domain saved.")
        if field == "emgmail":
            v = v.lower()
            if not re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", v):
                self.pending[self.uid] = (field, "", "")
                return "That doesn't look like an email address. Send your Gmail again.", [[("Cancel", "em:m")]]
            m["gmail"] = v
            self.s.save()
            return self.email_menu("✅ Gmail saved.")
        self.wipe_input = True                                           # the app password must not stay in the chat
        v = re.sub(r"\s+", "", v)
        if not m["gmail"]:
            return self.email_menu("Set the Gmail address first, then the App password.")
        if len(v) < 12:
            self.pending[self.uid] = (field, "", "")
            return "That is too short for a Google App password (16 letters). Send it again.", [[("Cancel", "em:m")]]
        bad = await catchall.test_login(m["gmail"], v)
        if bad:
            self.pending[self.uid] = (field, "", "")
            return f"❌ {html.escape(bad)}\nSend the App password again.", [[("Cancel", "em:m")]]
        m["app_password"] = seal(v)
        self.s.save()
        self.start_mail()
        return self.email_menu("✅ App password saved - Gmail login works. I delete your message.")

    def resume_watchers(self) -> None:
        """After a restart: settle numbers that were mid-check and keep watching every number that is still waiting for its code."""
        if self.mail_cfg():
            self.start_mail()
        for e in self.s.d["history"]:
            if e.get("chk") == "pending":
                e["chk"], e["chk_note"] = "unknown", "the bot restarted before the check finished"
            if e["state"] == "active" and e["idNum"] not in self.code_tasks and self.s.api_key:
                self.code_tasks[e["idNum"]] = asyncio.create_task(self.wait_code(e["idNum"]))

    async def release(self, id_num: str) -> Reply:
        e = self.find_purchase(id_num)
        if e is None:
            return "I don't know that number any more.", _nav(("📜 History", "hi:0"))
        if e["state"] in ("released", "code", "kept", "expired", "cancelled"):
            return self.purchase_card(e)
        client = self.client_factory()
        e["releasing"] = True
        try:
            try:
                st = await client.set_status(id_num, "end")
            except SmsCenterError as err:
                txt, kb = self.purchase_card(e)
                return txt + f"\n⚠️ Couldn't cancel: {html.escape(str(err))}", [*kb[:1], [("🔓 Try again", f"rl:{id_num}")]]
        finally:
            e.pop("releasing", None)
            await client.close()
        if st in ("ready", "update"):
            e["state"] = "released"
            t = self.code_tasks.pop(id_num, None)
            if t:
                t.cancel()
            self.s.save()
            if e.get("pinned"):                                          # cancelled -> not pinned any more
                await self.unpin_msg(e.get("mid"))
                e["pinned"] = False
                self.s.save()
            await self.refresh_card(e)
            return self.purchase_card(e)
        if st == "smsReceived":
            e["state"] = "code"
            self.s.save()
        txt, kb = self.purchase_card(e)
        return txt + f"\n⚠️ Can't cancel: SmsCenter says “{html.escape(st)}” (an SMS already arrived).", kb[:1]

    def history_screen(self, page: int = 0) -> Reply:
        hist = self.s.d["history"]
        if not hist:
            return "📜 <b>History</b>\nNothing bought yet. Make a hunter 🏹", [[("🏹 Hunter", "hu:l"), ("🏠 Menu", "m")]]
        per = 8
        pages = max(1, (len(hist) + per - 1) // per)
        page = max(0, min(page, pages - 1))
        part = hist[page * per:(page + 1) * per]
        icon = {"active": "🟡", "code": "🟢", "released": "🔓", "expired": "⌛", "kept": "⚠️", "cancelled": "🚫"}
        lines = []
        for e in part:
            what = f"code {e['code']}" if e["state"] == "code" else {"active": "waiting for the code", "released": "released", "expired": "no code - expired", "kept": "kept (couldn't release)", "cancelled": "ended on SmsCenter (timeout / panel)"}[e["state"]]
            ck = {"clean": "🟢", "banned": "🔴", "registered": "🟠"}.get(e.get("chk", "off"), "⚪")
            lines.append(f"{icon[e['state']]}{ck} {time.strftime('%d %b %H:%M', time.localtime(e['t']))} · +{e['tel']} · {e['sname']} {flag(e['cc'])} · {usd(e['price'])} · {what}")
        kb: KB = [[(f"🔓 Release +{e['tel']}", f"rl:{e['idNum']}")] for e in part if e["state"] == "active"]
        nav = []
        if page > 0:
            nav.append(("⬅️", f"hi:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(("➡️", f"hi:{page + 1}"))
        kb.append(nav)
        kb.append([("🏹 Hunter", "hu:l"), ("🏠 Menu", "m")])
        return f"📜 <b>History</b> (what the hunters bought)\n<pre>{html.escape(chr(10).join(lines))}</pre>", kb

    async def cmd_hunter(self, arg: str = "") -> Reply:
        return self.hunters_screen()

    async def cmd_history(self, arg: str = "") -> Reply:
        return self.history_screen(0)

    # -- price watch
    def watch_title(self, w: dict) -> str:
        return f"{html.escape(w['sname'])} under {usd(w['limit'])}"

    def watches_screen(self) -> Reply:
        ws = self.s.d["watches"]
        if not ws:
            return ("🎯 <b>Price watch</b>\nPick a platform and a price: I send you every country that is under it, then only what changes.\n"
                    "Example: <code>/watch tg 0.30</code>"), [[("➕ New watch", "pw:n:0")], [("🏠 Menu", "m")]]
        kb = []
        for wid, w in sorted(ws.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
            kb.append([(f"{'▶️' if w.get('on') else '⏸'} {w['sname'][:14]} < {usd(w['limit'])} · {len(w.get('under') or {})} under", f"pw:o:{wid}")])
        kb.append([("➕ New watch", "pw:n:0"), ("🏠 Menu", "m")])
        return f"🎯 <b>Price watches</b> ({len(ws)}) - tap one to see the countries under its price and to change it.", kb

    async def watch_platforms(self, page: int) -> Reply:
        svcs = await self.load_services()
        items = sorted(svcs.items(), key=lambda kv: (kv[0] != DEFAULT_SVC, kv[1].lower()))
        pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
        page = max(0, min(page, pages - 1))
        part = items[page * PER_PAGE:(page + 1) * PER_PAGE]
        have = {w["svc"] for w in self.s.d["watches"].values()}
        kb: KB = [[("🔎 Search platform", "pw:q")]]
        for i in range(0, len(part), 2):
            kb.append([(f"{'🎯 ' if code in have else ''}{name[:20]}", f"pw:s:{code}") for code, name in part[i:i + 2]])
        nav = []
        if page > 0:
            nav.append(("⬅️", f"pw:n:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(("➡️", f"pw:n:{page + 1}"))
        kb.append(nav)
        kb.append([("⬅️ Watches", "pw:l"), ("🏠 Menu", "m")])
        return "🎯 <b>New price watch</b>\nStep 1: pick the platform. Step 2: send the price.", kb

    def watch_card(self, wid: str) -> Reply:
        w = self.s.d["watches"].get(wid)
        if w is None:
            return "That watch doesn't exist any more.", [[("⬅️ Watches", "pw:l"), ("🏠 Menu", "m")]]
        under, names = w.get("under") or {}, w.get("names") or {}
        age = "waiting for the first scan…" if not w.get("last_scan") else f"last scan {int(time.time() - w['last_scan'])}s ago"
        head = (f"🎯 <b>{self.watch_title(w)}</b>   {'▶️ running' if w.get('on') else '⏸ paused'}\n"
                f"Price: {BASIS[w.get('basis', 'api')]} · every {secs(self.s.watch_interval_of(w))} · min stock {int(w.get('minstock') or 1)} · {age}\n")
        if not w.get("seeded"):
            body = "The full list of countries under this price is sent as soon as the first scan finishes."
        elif not under:
            body = "No country is under this price right now."
        else:
            rows = sorted(under.items(), key=lambda kv: (kv[1], kv[0]))[:30]
            body = f"<b>{len(under)}</b> countries under {usd(w['limit'])} now:\n" + self.tracker.watch_lines(names, [(cc, usd(p)) for cc, p in rows]) + \
                   (f"\n…and {len(under) - 30} more" if len(under) > 30 else "")
        cur = self.s.watch_interval_of(w)
        kb = [[("✏️ Change price", f"pw:w:price:{wid}"), (f"📊 {BASIS[w.get('basis', 'api')]} ⇄", f"pw:b:{wid}")],
              [(f"📦 Min stock: {int(w.get('minstock') or 1)}", f"pw:w:stock:{wid}")],
              [(("✅ " if abs(cur - v) < 1e-9 else "") + secs(v), f"pw:i:{wid}:{v}") for v in WATCH_PRESETS] + [("⏱", f"pw:w:int:{wid}")],
              [("⏸ Pause" if w.get("on") else "▶️ Start", f"pw:t:{wid}"), ("📋 Send full list now", f"pw:r:{wid}")],
              [("🗑 Delete", f"pw:x:{wid}"), ("⬅️ Watches", "pw:l"), ("🏠 Menu", "m")]]
        return head + body, kb

    async def create_watch(self, svc: str, limit: float, basis: str = "api") -> Reply:
        if (r := self.need_key()):
            return r
        svcs = await self.load_services()
        ws = self.s.d["watches"]
        wid = str(max([int(k) for k in ws if k.isdigit()] or [0]) + 1)
        ws[wid] = new_watch(wid, svc, svcs.get(svc, svc.upper()), limit)
        ws[wid]["basis"] = basis
        self.s.d["service"] = svc
        self.s.save()
        self.tracker.wnext.pop(wid, None)
        self.tracker.poke(force=True)
        scr = self.watch_card(wid)
        return "✅ Watch started - scanning every country now…\n\n" + scr[0], scr[1]

    def reseed(self, w: dict) -> None:
        """Start the watch from scratch: the next scan sends the full list again."""
        w["seeded"], w["under"], w["names"] = False, {}, {}
        self.tracker.wnext.pop(w["id"], None)
        self.tracker.poke(force=True)

    async def ui_watch(self, uid: int, p: list[str]) -> Reply | None:
        sub, ws = p[1], self.s.d["watches"]
        if sub == "l":
            return self.watches_screen()
        if sub == "n":
            return await self.watch_platforms(int(p[2]))
        if sub == "q":
            self.pending[uid] = ("wpsearch", "", "")
            return "🔎 <b>Search platform</b>\nType a platform name or its code (e.g. <code>whatsapp</code>, <code>wa</code>).", [[("⬅️ Platforms", "pw:n:0")]]
        if sub == "s":                                                   # platform chosen -> ask for the price
            if (r := self.need_key()):
                return r
            svc = p[2]
            sname = (await self.load_services()).get(svc, svc.upper())
            self.pending[uid] = ("wprice", svc, "")
            return (f"🎯 <b>{html.escape(sname)}</b>\nSend the price: I'll list every country under it (example <code>0.30</code>).",
                    [[(f"${v}", f"pw:c:{svc}:{v}") for v in ("0.10", "0.25", "0.50", "1.00")], [("⬅️ Platforms", "pw:n:0")]])
        if sub == "c":                                                   # a quick price button
            return await self.create_watch(p[2], float(p[3]))
        if sub == "w":                                                   # type a value: pw:w:<price|int>:<wid>
            field, wid = p[2], p[3]
            if wid not in ws:
                return self.watches_screen()
            self.pending[uid] = ({"price": "wlimit", "stock": "wstock"}.get(field, "wint"), wid, "")
            what = ("the new price limit, like <code>0.30</code>" if field == "price"
                    else "the minimum stock a price needs to count, like <code>1</code> (anything) or <code>20</code> (ignore tiny stocks)" if field == "stock"
                    else f"how often to scan, in seconds (minimum {secs(WATCH_MIN)}): <code>60</code>, <code>2m</code> or <code>default</code>")
            return f"{self.watch_title(ws[wid])}: send {what}.", [[("Cancel", f"pw:o:{wid}")]]
        wid = p[2] if len(p) > 2 else ""
        w = ws.get(wid)
        if w is None:
            return self.watches_screen()
        if sub == "o":
            return self.watch_card(wid)
        if sub == "t":
            w["on"] = not w.get("on")
            if w["on"]:
                self.reseed(w)                                           # coming back from a pause: full list again
            self.s.save()
        elif sub == "b":
            w["basis"] = "min" if w.get("basis", "api") == "api" else "api"
            self.reseed(w)
            self.s.save()
        elif sub == "i":
            w["interval"] = max(WATCH_MIN, float(p[3]))
            self.tracker.wnext.pop(wid, None)
            self.tracker.poke()
            self.s.save()
        elif sub == "r":
            self.reseed(w)
            self.s.save()
        elif sub == "x":
            return f"Delete the watch <b>{self.watch_title(w)}</b>?", [[("✅ Yes, delete", f"pw:xy:{wid}"), ("Cancel", f"pw:o:{wid}")]]
        elif sub == "xy":
            ws.pop(wid, None)
            self.tracker.wnext.pop(wid, None)
            self.s.save()
            return self.watches_screen()
        return self.watch_card(wid)

    async def take_watch_value(self, uid: int, field: str, a: str, text: str) -> Reply:
        t = text.strip().lower().replace(",", ".").lstrip("$")
        if field == "wprice":                                            # a = platform code
            try:
                v = float(t)
                if v <= 0:
                    raise ValueError
            except ValueError:
                self.pending[uid] = ("wprice", a, "")
                return "Send a price like <code>0.30</code>.", [[("⬅️ Platforms", "pw:n:0")]]
            return await self.create_watch(a, v)
        w = self.s.d["watches"].get(a)                                   # a = watch id
        if w is None:
            return self.watches_screen()
        if field == "wstock":
            try:
                n = int(float(t))
                if n < 1:
                    raise ValueError
            except ValueError:
                self.pending[uid] = ("wstock", a, "")
                return "Send a whole number, 1 or more (e.g. <code>20</code>).", [[("Cancel", f"pw:o:{a}")]]
            w["minstock"] = n
            self.reseed(w)
        elif field == "wlimit":
            try:
                v = float(t)
                if v <= 0:
                    raise ValueError
            except ValueError:
                self.pending[uid] = ("wlimit", a, "")
                return "Send a price like <code>0.30</code>.", [[("Cancel", f"pw:o:{a}")]]
            w["limit"] = v
            self.reseed(w)
        else:
            if t in ("default", "reset", "off"):
                w["interval"] = None
            else:
                v = parse_secs(text)
                if v is None or v < WATCH_MIN:
                    self.pending[uid] = ("wint", a, "")
                    return f"Send a number of seconds, at least {secs(WATCH_MIN)} (e.g. <code>60</code>), or <code>default</code>.", [[("Cancel", f"pw:o:{a}")]]
                w["interval"] = v
            self.tracker.wnext.pop(a, None)
            self.tracker.poke()
        self.s.save()
        return self.watch_card(a)

    async def cmd_watch(self, arg: str) -> Reply:
        parts = arg.split()
        if not parts:
            return self.watches_screen()
        try:
            limit = float(parts[-1].replace(",", ".").lstrip("$"))
            if limit <= 0:
                raise ValueError
        except ValueError:
            return "Usage: /watch [platform] &lt;price&gt;   e.g. /watch tg 0.30   or   /watch whatsapp 0.5", _nav()
        svcs = await self.load_services()
        name = " ".join(parts[:-1])
        svc = self.find_service(name, svcs) if name else (self.s.d.get("service") or DEFAULT_SVC)
        if not svc:
            return f"I don't know the platform “{html.escape(name)}”. Try /platforms.", _nav()
        return await self.create_watch(svc, limit)

    async def cmd_watches(self, arg: str = "") -> Reply:
        return self.watches_screen()

    async def cmd_unwatch(self, arg: str) -> Reply:
        a, ws = arg.strip().lower(), self.s.d["watches"]
        if a == "all":
            ws.clear()
        elif a in ws:
            ws.pop(a)
        else:
            return "Usage: /unwatch &lt;id|all&gt;   (ids are in /watches)", _nav()
        self.s.save()
        return self.watches_screen()

    # -- typed commands
    async def cmd_menu(self, arg: str = "") -> Reply:
        return self.main_screen()

    async def cmd_help(self, arg: str = "") -> Reply:
        return self.HELP, _nav()

    async def _target(self, text: str) -> tuple[str | None, dict | None, Reply | None]:
        """'[platform] <country>' -> (platform code, country, error reply)."""
        if (r := self.need_key()):
            return None, None, r
        try:
            svcs = await self.load_services()
            svc, words = self.s.d.get("service") or DEFAULT_SVC, text.split()
            if len(words) > 1 and (sv := self.find_service(words[0], svcs, fuzzy=False)):
                svc, text = sv, " ".join(words[1:])
            c = await self.resolve(text)
        except SmsCenterError as e:
            return None, None, (f"SmsCenter error: {html.escape(str(e))}", _nav())
        if not c:
            return None, None, ("I don't know that country. Try /countries or type part of its name.", [[("📱 Platforms", "pl:0")]])
        return svc, c, None

    async def cmd_platforms(self, arg: str = "") -> Reply:
        return await self.platforms_screen(0)

    cmd_services = cmd_platforms

    async def cmd_countries(self, arg: str = "") -> Reply:
        svc = self.find_service(arg, await self.load_services()) if arg.strip() else None
        return await self.countries_screen(svc or self.s.d.get("service") or DEFAULT_SVC, 0)

    async def cmd_prices(self, arg: str = "") -> Reply:
        if not arg.strip():
            return await self.countries_screen(self.s.d.get("service") or DEFAULT_SVC, 0)
        svc, c, err = await self._target(arg)
        return err or await self.card(svc, c["code"])

    async def cmd_add(self, arg: str) -> Reply:
        if not arg.strip():
            return "Usage: /add [platform] &lt;country&gt;   e.g. /add telegram ukraine   or   /add ua\nOr use 📱 Platforms.", [[("📱 Platforms", "pl:0")]]
        svc, c, err = await self._target(arg)
        if err:
            return err
        await self.ensure(svc, c["code"])
        self.set_on(svc, c["code"], True)
        return await self.card(svc, c["code"])

    async def cmd_stop(self, arg: str) -> Reply:
        svc, c, err = await self._target(arg)
        if err:
            return err
        if tkey(svc, c["code"]) in self.s.d["tracks"]:
            self.set_on(svc, c["code"], False)
        return await self.card(svc, c["code"])

    async def cmd_remove(self, arg: str) -> Reply:
        svc, c, err = await self._target(arg)
        if err:
            return err
        self.drop_track(svc, c["code"])
        return f"Removed {flag(c['code'])} {html.escape(c['name'])} · {html.escape((await self.load_services()).get(svc, svc.upper()))}.", _nav()

    async def cmd_set(self, arg: str) -> Reply:
        m = re.match(r"^(.+?)\s+(any|drop|under|every)\s*(.*)$", arg.strip(), re.I)
        usage = ("Usage:\n/set [platform] &lt;country&gt; any on|off\n/set [platform] &lt;country&gt; drop 0.05 | 10% | off | default\n"
                 "/set [platform] &lt;country&gt; under 1.20 | off\n/set [platform] &lt;country&gt; every 10 | default   (seconds)")
        if not m:
            return usage, _nav()
        svc, c, err = await self._target(m.group(1))
        if err:
            return err
        t = await self.ensure(svc, c["code"])
        cc, field, val = c["code"], m.group(2).lower(), m.group(3).strip().lower()
        if field == "any":
            if val not in ("on", "off"):
                return "Usage: /set [platform] &lt;country&gt; any on|off", _nav()
            t["any"] = val == "on"
        elif field == "drop":
            r = parse_drop(val)
            if not r:
                return "Usage: /set [platform] &lt;country&gt; drop 0.05 | 10% | off | default", _nav()
            t["drop"] = "off" if r[0] == "off" else (None if r[0] == "default" else {r[0]: r[1]})
        elif field == "every":
            if val in ("default", "reset", "off"):
                t["interval"] = None
            else:
                v = parse_secs(val)
                if v is None or v < MIN_INTERVAL:
                    return f"Usage: /set [platform] &lt;country&gt; every &lt;seconds, min {secs(MIN_INTERVAL)}&gt; | default", _nav()
                t["interval"] = v
            self.tracker.next.pop(tkey(svc, cc), None)
            self.tracker.poke()
        else:
            try:
                t["under"] = None if val in ("off", "no", "none") else float(val.replace(",", "."))
                if t["under"] is not None and t["under"] <= 0:
                    raise ValueError
            except ValueError:
                return "Usage: /set [platform] &lt;country&gt; under 1.20 | off", _nav()
            t["armed"] = True
        self.s.save()
        return await self.card(svc, cc)

    async def cmd_list(self, arg: str = "") -> Reply:
        return self.tracked_screen()

    async def cmd_trend(self, arg: str) -> Reply:
        svc, c, err = await self._target(arg)
        if err:
            return err
        h = self.s.d["hist"].get(tkey(svc, c["code"]), [])
        if not h:
            return "No history yet for that one (it builds up while it is tracked).", _nav()
        lines, prev = [], None
        for ts, mn, api, lst, cnt, stock in h[-12:]:
            cur = mn or api or lst or cnt
            arrow = "" if prev is None or cur is None else (" 📉" if cur < prev else " 📈" if cur > prev else "")
            lines.append(f"{time.strftime('%d %b %H:%M:%S', time.localtime(ts))} · cheapest {fmt(mn)} · API {fmt(api)}/{fmt(lst)}/{fmt(cnt)} · {stock} in stock{arrow}")
            prev = cur if cur is not None else prev
        sname = (await self.load_services()).get(svc, svc.upper())
        return f"📈 {flag(c['code'])} <b>{html.escape(c['name'])}</b> · {html.escape(sname)} · price history\n" + "\n".join(lines), _nav()

    async def cmd_drop(self, arg: str) -> Reply:
        r = parse_drop(arg)
        if not r or r[0] != "pct":
            return "Usage: /drop &lt;percent&gt;   e.g. /drop 5  (the default for tracks without their own setting)", _nav()
        self.s.d["drop_pct"] = r[1]
        self.s.save()
        return f"Default drop alert: {r[1]:g}%.", _nav()

    async def cmd_interval(self, arg: str) -> Reply:
        v = parse_secs(arg)
        if v is None or v < MIN_INTERVAL:
            return f"Usage: /interval &lt;seconds&gt;   (minimum {secs(MIN_INTERVAL)}; e.g. /interval 10, /interval 2m)", _nav()
        self.s.d["interval"] = v
        self.s.save()
        self.tracker.poke()
        return f"Default: checking every {secs(v)} (tracks with their own interval keep it).", _nav()

    async def cmd_mute(self, arg: str) -> Reply:
        v = arg.strip().lower()
        if v in ("off", "0"):
            self.s.d["mute_until"] = 0.0
            self.s.save()
            return "🔔 Unmuted.", _nav()
        try:
            m = int(v)
            if m <= 0:
                raise ValueError
        except ValueError:
            return "Usage: /mute &lt;minutes&gt;   or   /mute off", _nav()
        self.s.d["mute_until"] = time.time() + m * 60
        self.s.save()
        return f"🔕 Price-change alerts muted for {m} min (price-limit 🎯 alerts still arrive).", _nav()

    async def cmd_setkey(self, arg: str) -> Reply:
        k = arg.strip()
        if len(k) < 8:
            return "Usage: /setkey &lt;your SmsCenter API key&gt;", _nav()
        if await check_key(k) is None:
            return "SmsCenter rejected that key (or could not be reached). Check it and try again.", _nav()
        self.s.set_secret("api_key", k)
        self._countries, self._services = [], None
        self.tracker.poke()
        return "API key saved (I deleted your message). Now pick a platform 👇", [[("📱 Platforms", "pl:0")], [("🏠 Menu", "m")]]

    async def cmd_status(self, arg: str = "") -> Reply:
        return self.main_screen()

    async def cmd_pause(self, arg: str = "") -> Reply:
        self.s.d["paused"] = True
        self.s.save()
        return "Paused. /resume to continue.", _nav()

    async def cmd_resume(self, arg: str = "") -> Reply:
        self.s.d["paused"] = False
        self.s.save()
        self.tracker.poke()
        return "Resumed.", _nav()

    async def cmd_check(self, arg: str = "") -> Reply:
        if (r := self.need_key()):
            return r
        if not self.n_on():
            return "Nothing is being tracked yet.", [[("📱 Platforms", "pl:0")]]
        self.tracker.poke(force=True)
        return "🔄 Checking now...", _nav()

    # -- search by typing
    async def search_countries(self, text: str, svc: str) -> Reply:
        if (r := self.need_key()):
            return r
        t = text.strip().lower()
        try:
            hits = [x for x in await self.load_countries() if t in x["name"].lower() or t == x["code"]][:12]
        except SmsCenterError as e:
            return f"SmsCenter error: {html.escape(str(e))}", _nav()
        if not hits:
            return f"No country matches “{html.escape(text.strip())}”.", [[("⬅️ Countries", f"pc:{svc}:0")]]
        if len(hits) == 1:
            return await self.card(svc, hits[0]["code"])
        tracks = self.s.d["tracks"]
        kb = [[(f"{'✅ ' if tracks.get(tkey(svc, x['code']), {}).get('on') else ''}{flag(x['code'])} {x['name'][:18]}", f"v:{svc}:{x['code']}") for x in hits[i:i + 2]]
              for i in range(0, len(hits), 2)]
        kb.append([("🏠 Menu", "m")])
        return f"🔎 {len(hits)} matches - tap one", kb

    async def search_services(self, text: str, watch: bool = False) -> Reply:
        t = text.strip().lower()
        svcs = await self.load_services()
        pick = (lambda c: f"pw:s:{c}") if watch else (lambda c: f"pc:{c}:0")
        back = ("⬅️ Platforms", "pw:n:0" if watch else "pl:0")
        hits = [(c, n) for c, n in svcs.items() if t == c or t in n.lower()][:12]
        if not hits:
            kb: KB = [[back]]
            if re.fullmatch(r"[a-z0-9_]{1,16}", t):
                kb.insert(0, [(f"Use code “{t}” anyway", pick(t))])
            return f"No platform matches “{html.escape(text.strip())}”.", kb
        if len(hits) == 1:
            if watch:
                return await self.ui_watch(self.uid, ["pw", "s", hits[0][0]])
            return await self.countries_screen(hits[0][0], 0)
        kb = [[(f"{'✅ ' if self.n_on(c) else ''}{n[:20]}", pick(c)) for c, n in hits[i:i + 2]] for i in range(0, len(hits), 2)]
        kb.append([("🏠 Menu", "m")])
        return f"🔎 {len(hits)} platforms match - tap one", kb

    async def handle(self, uid: int, text: str) -> Reply | None:
        """Route one typed message. None = ignore silently (not the owner)."""
        text = text.strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/start":
            scr = self.main_screen()
            if self.s.d.get("paused") and self.s.d["tracks"]:
                self.s.d["paused"] = False                               # they are back (see notify)
                self.s.save()
                self.tracker.poke()
            return "👋 " + scr[0] + ("" if self.s.api_key else "\n\nFirst send <b>your own</b> SmsCenter key: <code>/setkey YOUR_KEY</code>"), scr[1]
        if not cmd.startswith("/"):
            pend = self.pending.get(uid)
            if pend and pend[0] == "wpsearch":
                res = await self.search_services(text, watch=True)
                if res[0].startswith("No platform matches"):
                    self.pending[uid] = ("wpsearch", "", "")
                elif self.pending.get(uid, ("",))[0] == "wpsearch":
                    self.pending.pop(uid, None)                          # (a single hit already set the price question itself)
                return res
            if pend and pend[0] == "psearch":
                res = await self.search_services(text)
                if res[0].startswith("No platform matches"):             # stay in search mode so the next try just works
                    self.pending[uid] = ("psearch", "", "")
                else:
                    self.pending.pop(uid, None)
                return res
            if pend and pend[0] == "search":
                res = await self.search_countries(text, pend[1])
                if res[0].startswith("No country matches"):
                    self.pending[uid] = pend
                else:
                    self.pending.pop(uid, None)
                return res
            if pend:
                return await self.take_value(uid, text)
            res = await self.search_countries(text, self.s.d.get("service") or DEFAULT_SVC)   # plain text: a country first, then a platform
            if res[0].startswith("No country matches"):
                res2 = await self.search_services(text)
                if not res2[0].startswith("No platform matches"):
                    return res2
            return res
        self.pending.pop(uid, None)
        fn = getattr(self, "cmd_" + cmd.lstrip("/"), None)
        if fn is None:
            return await self.cmd_help()
        try:
            return await fn(arg)
        except Exception as e:                                           # noqa: BLE001
            log.exception("command failed")
            return f"Something went wrong: {html.escape(type(e).__name__)}", _nav()


ADMIN_CMDS = {"/access", "/allow", "/deny", "/users", "/session", "/proxies"}
LOGIN_TTL = 300                                                          # a half-finished Telethon login is forgotten after this many seconds
SESSION_BASE = os.path.join(HERE, "gh_checker")                          # -> gh_checker.session


class Hub:
    """Many users: one App per Telegram user (own profile, own SmsCenter key, own tracker loop). The admin controls who may use the bot."""

    def __init__(self, store: Store, bot=None):
        self.store, self.bot = store, bot
        self.apps: dict[int, App] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self.login_logs: dict[int, list[str]] = {}                       # admin uid -> last login events (class names/types only, never secrets)
        self.logins: dict[int, dict] = {}                                # admin uid -> half-finished Telethon login {client, phone, hash, stage, timer}
        self.mail_task: asyncio.Task | None = None              # the one watcher of the catch-all Gmail, for every user
        self.mail_err = ""

    def start_mail(self) -> None:
        if self.mail_task is None or self.mail_task.done():
            self.mail_task = asyncio.create_task(self.mail_loop())

    async def mail_loop(self) -> None:
        w = catchall.MailWatch()
        try:
            while True:
                cfg = self.store.mail_cfg()
                if not cfg:
                    w.close()
                    await asyncio.sleep(10)
                    continue
                delay = 6.0
                try:
                    owners = self.store.mail_owners()
                    for it in await asyncio.to_thread(w.poll, cfg[0], cfg[1], frozenset(owners)):
                        uid = owners.get(it["to"])
                        if uid is not None and self.allowed(uid):
                            await self.app(uid).mail_notify(it)
                    self.mail_err = ""
                except Exception as e:                                   # noqa: BLE001
                    w.close()
                    delay = 30.0
                    err = f"{type(e).__name__}: {str(e)[:100]}"
                    log.warning("mail watcher: %s", err)
                    if err != self.mail_err:
                        self.mail_err = err
                        await self.app(self.store.d["admin"]).notify(f"⚠️ Email watcher can't read your Gmail ({html.escape(err)}). I keep retrying - check the App password in 📧 Email.")
                await asyncio.sleep(delay)
        finally:
            w.close()

    def allowed(self, uid: int) -> bool:
        d = self.store.d
        if d["admin"] == uid:
            return True
        return uid not in d["blocked"] and (d["access"] == "open" or uid in d["allowed"])

    def app(self, uid: int) -> App:
        if uid not in self.apps:
            a = self.apps[uid] = App(UserState(self.store, uid), self.bot, uid)
            a.hub = self
            self.tasks[uid] = asyncio.create_task(a.tracker.run())
            a.resume_watchers()
            asyncio.create_task(a.warm())
        return self.apps[uid]

    def stop(self, uid: int) -> None:
        app = self.apps.pop(uid, None)
        if app:
            app.shutdown()                                               # hunters and code waiters stop with the user
        task = self.tasks.pop(uid, None)
        if task:
            task.cancel()

    def sync(self) -> None:
        """After an access change: stop the tracking of users who are no longer allowed."""
        for uid in list(self.apps):
            if not self.allowed(uid):
                self.stop(uid)

    def apply_proxies(self) -> None:
        """Telethon proxies: the SMSCENTER_TG_PROXIES secret wins, else the list the admin gave with /proxies (stored sealed)."""
        if not os.environ.get("SMSCENTER_TG_PROXIES", "").strip() and self.store.d.get("proxies"):
            os.environ["SMSCENTER_TG_PROXIES"] = unseal(self.store.d["proxies"])

    def start_saved(self) -> None:
        """Resume tracking for every user already in the file."""
        self.apply_proxies()
        for k in list(self.store.d["users"]):
            if k.lstrip("-").isdigit() and self.allowed(int(k)):
                self.app(int(k))

    def shutdown(self) -> None:
        for uid in list(self.tasks):
            self.stop(uid)
        if self.mail_task:
            self.mail_task.cancel()
            self.mail_task = None

    async def handle(self, uid: int, text: str) -> Reply | None:
        d = self.store.d
        head = text.strip().split(None, 1)                               # a command may be followed by a space or a new line
        cmd, arg = (head[0] if head else ""), (head[1] if len(head) > 1 else "")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/start" and d["admin"] is None:                       # the first person to /start is the admin
            d["admin"] = uid
            if d.get("legacy"):                                          # settings from the old one-user version go to the admin
                d["users"][str(uid)] = d["legacy"]
                d["legacy"] = None
            self.store.save()
            res = await self.app(uid).handle(uid, text)
            return ("👑 You are the <b>admin</b> of this bot (/users · /access · /allow · /deny).\n\n" + res[0], res[1]) if res else res
        if not self.allowed(uid):
            return f"This bot is private. Ask the admin to allow your ID: <code>{uid}</code>", None
        if cmd in ("/session", "/proxies") and uid != d["admin"]:
            return "Only the admin can do that.", None
        if cmd == "/proxies":
            self.app(uid).wipe_input = True                              # the list holds passwords: delete the message
        if uid == d["admin"] and cmd in ADMIN_CMDS:
            return await self.admin(cmd, arg)
        if uid == d["admin"] and uid in self.logins and not cmd.startswith("/"):
            return await self.login_step(uid, text)
        return await self.app(uid).handle(uid, text)

    async def ui(self, uid: int, data: str) -> Reply | None:
        if not self.allowed(uid):
            return None
        if data.startswith("sess:"):
            if uid != self.store.d["admin"]:
                return None
            if data == "sess:x":
                await self.login_drop(uid)
                return "Cancelled.", _nav()
            if data == "sess:qr":
                return await self.login_qr(uid)
            if data == "sess:log":
                return self.login_log(uid), [[("📜 Refresh", "sess:log"), ("Cancel", "sess:x")]]
            if data in ("sess:sms", "sess:call"):
                return await self.login_resend(uid, data[5:])
            return await self.login_start(uid)
        return await self.app(uid).ui(uid, data)

    # -- new Telethon session for the number checker, created from the admin's chat (nothing secret goes through workflow inputs or logs)
    def login_note(self, uid: int, msg: str) -> None:
        """Record a login event for the admin's 📜 Log button and the Actions log (never put phone/code/hash/exception text in msg)."""
        buf = self.login_logs.setdefault(uid, [])
        buf.append(time.strftime("%H:%M:%S", time.gmtime()) + " " + msg)
        del buf[:-25]
        log.info("session login: %s", msg)

    def login_log(self, uid: int) -> str:
        return "📜 <b>Login log</b> (UTC)\n" + ("\n".join(html.escape(x) for x in self.login_logs.get(uid, [])) or "nothing yet - send /session")

    async def login_drop(self, uid: int) -> None:
        st = self.logins.pop(uid, None)
        if st:
            if st.get("timer"):
                st["timer"].cancel()
            if st.get("qr_task"):
                st["qr_task"].cancel()
            if st.get("client"):
                try:
                    await st["client"].disconnect()
                except Exception:                                        # noqa: BLE001
                    pass

    def login_arm(self, uid: int, st: dict) -> None:
        if st.get("timer"):
            st["timer"].cancel()
        st["timer"] = asyncio.get_running_loop().call_later(LOGIN_TTL, lambda: asyncio.create_task(self.login_expire(uid, st)))

    async def login_expire(self, uid: int, st: dict) -> None:
        if self.logins.get(uid) is not st:
            return
        await self.login_drop(uid)
        try:
            await self.bot.send_message(uid, "⏱ The Telegram login timed out (5 min). Send /session to start again.")
        except Exception:                                                # noqa: BLE001
            pass

    async def login_start(self, uid: int) -> Reply:
        await self.login_drop(uid)
        self.login_logs[uid] = []
        self.login_note(uid, "login started")
        st = self.logins[uid] = {"client": None, "phone": "", "hash": "", "stage": "phone", "timer": None}
        self.login_arm(uid, st)
        self.app(uid).wipe_input = False
        return ("🔐 <b>New checker session</b>\nSend the phone number of the Telegram account the checker should use, in international format (<code>+15551234567</code>). "
                "I delete your message right after.\n<i>The old session file is replaced.</i>\n\nNo code arriving? Use QR login instead."), [[("🔳 Log in by QR (no code)", "sess:qr")], [("Cancel", "sess:x")]]

    @staticmethod
    def code_where(sent: Any, phone: str) -> tuple[str, list]:
        """Plain-words 'where to look' text for a SentCode, and the resend buttons its next_type allows (no code/hash/full phone)."""
        def kind(t: Any) -> str:
            return type(t).__name__.replace("SentCodeType", "") if t is not None else ""
        k, nk = kind(getattr(sent, "type", None)), kind(getattr(sent, "next_type", None))
        tail = html.escape(phone[-2:])
        where = {"App": "in the <b>Telegram app</b>, in the chat named <b>Telegram</b> (open this account on another device)",
                 "Sms": f"by <b>SMS</b> to the number ending in <code>{tail}</code>",
                 "FirebaseSms": f"by <b>SMS</b> to the number ending in <code>{tail}</code>",
                 "Call": "by <b>phone call</b> (a voice reads the code)",
                 "FlashCall": "by a <b>flash call</b> (a missed call whose number is the code)",
                 "MissedCall": "by a <b>missed call</b> (the last digits of the caller's number are the code)",
                 "EmailCode": "by <b>email</b> to the address set on that account"}.get(k, f"by an unusual method (<code>{html.escape(k or 'unknown')}</code>)")
        nxt = {"Sms": "an SMS", "Call": "a phone call", "FlashCall": "a flash call", "MissedCall": "a missed call"}.get(nk)
        text = f"📨 Telegram says it sent the login code {where}."
        if nxt:
            text += f"\nIf it doesn't arrive, the next option is {nxt} (use the button below)."
        else:
            text += "\nNo other delivery method is available now; if nothing arrives, wait a few minutes and start again."
        row = []
        if nk == "Sms":
            row.append(("📩 Resend by SMS", "sess:sms"))
        if nk == "Call":
            row.append(("📞 Call me", "sess:call"))
        return text, ([row] if row else []) + [[("🔳 Log in by QR (no code)", "sess:qr")], [("📜 Log", "sess:log"), ("Cancel", "sess:x")]]

    async def login_resend(self, uid: int, how: str) -> Reply:
        st = self.logins.get(uid)
        if not st or st.get("stage") != "code" or not st.get("client"):
            return "No login is waiting for a code. Send /session to start.", _nav()
        self.login_arm(uid, st)
        try:
            from telethon.errors import FloodWaitError
            from telethon.tl.functions.auth import ResendCodeRequest
            sent = await st["client"](ResendCodeRequest(st["phone"], st["hash"]))
            st["hash"] = sent.phone_code_hash
            self.login_note(uid, f"resend ({how}): type={type(sent.type).__name__} next={type(sent.next_type).__name__} timeout={getattr(sent, 'timeout', None)}")
            text, kb = self.code_where(sent, st["phone"])
            return text + "\nType it with <b>spaces or dashes</b> between the digits, e.g. <code>1 2 3 4 5</code>.", kb
        except FloodWaitError as e:
            self.login_note(uid, f"FloodWaitError {e.seconds}s")
            return f"⏳ Telegram says wait {e.seconds} seconds before asking for another code.", [[("Cancel", "sess:x")]]
        except Exception as e:                                           # noqa: BLE001
            await self.login_drop(uid)
            self.login_note(uid, "resend failed: " + type(e).__name__)
            return f"❌ Could not resend the code: {html.escape(type(e).__name__)}. Send /session to start again.", _nav()

    async def login_client(self, uid: int, st: dict) -> bool:
        """Connect the Telethon client of this login (new session file, through the proxies); False when no api id/hash is set."""
        if st.get("client"):
            return True
        cr = PhoneChecker.creds()
        if not cr:
            await self.login_drop(uid)
            return False
        await CHECKER.close()                                            # it holds the old file open
        for f in (SESSION_BASE + ".session", SESSION_BASE + ".session-journal"):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
        from tgbot_checker import connect_client
        st["client"] = await connect_client(SESSION_BASE, cr[0], cr[1], lambda m: self.login_note(uid, m), shuffle=True)
        return True

    async def login_finish(self, uid: int, st: dict) -> Reply:
        me = await st["client"].get_me()
        try:
            st["client"].session.save()
        except Exception:                                                # noqa: BLE001
            pass
        self.login_note(uid, "signed in, session saved")
        for mid in st.get("qr_msgs", []):
            try:
                await self.bot.delete_message(uid, mid)                  # the QR is a login token: don't leave it in the chat
            except Exception:                                            # noqa: BLE001
                pass
        await self.login_drop(uid)                                       # disconnect closes the session file
        CHECKER.err, CHECKER.retry_at = "", 0.0
        await CHECKER.close()                                            # reconnects with the new file on the next check
        who = html.escape(me.first_name or "") + (f" (@{html.escape(me.username)})" if me.username else "")
        return f"✅ Checker session created for <b>{who.strip()}</b>.", _nav()

    async def login_qr(self, uid: int) -> Reply:
        """Log in without a code: the admin scans a QR from a Telegram app already logged into the account (Settings > Devices > Link Desktop Device)."""
        st = self.logins.get(uid)
        if not st or st.get("stage") not in ("phone", "code"):
            return "No login in progress. Send /session to start.", _nav()
        self.login_arm(uid, st)
        try:
            import segno  # noqa: F401
            if not await self.login_client(uid, st):
                return "No Telegram api id/hash configured (SMSCENTER_TG_API_ID / SMSCENTER_TG_API_HASH).", _nav()
            st["qr"] = await st["client"].qr_login()
            st["stage"] = "qr"
            self.login_note(uid, "QR login started")
            st["qr_task"] = asyncio.create_task(self.login_qr_loop(uid, st))
        except Exception as e:                                           # noqa: BLE001
            await self.login_drop(uid)
            self.login_note(uid, "QR start failed: " + type(e).__name__)
            return f"❌ Could not start QR login: {html.escape(type(e).__name__)}. Send /session to start again.", _nav()
        return ("🔳 <b>QR login</b>\nOn the phone where this account is logged in: Telegram → Settings → Devices → <b>Link Desktop Device</b> → scan the QR I send next. "
                "It refreshes every ~30 s."), [[("📜 Log", "sess:log"), ("Cancel", "sess:x")]]

    async def login_qr_loop(self, uid: int, st: dict) -> None:
        import io
        import segno
        from aiogram.types import BufferedInputFile
        from telethon.errors import SessionPasswordNeededError
        st["qr_msgs"] = []
        done: Reply | None = None
        try:
            while self.logins.get(uid) is st and st.get("stage") == "qr":
                buf = io.BytesIO()
                segno.make(st["qr"].url, error="m").save(buf, kind="png", scale=8, border=3)
                m = await self.bot.send_photo(uid, BufferedInputFile(buf.getvalue(), "qr.png"), caption="Scan me: Settings → Devices → Link Desktop Device")
                for old in st["qr_msgs"]:
                    try:
                        await self.bot.delete_message(uid, old)
                    except Exception:                                    # noqa: BLE001
                        pass
                st["qr_msgs"] = [m.message_id]
                try:
                    await st["qr"].wait(30)
                except asyncio.TimeoutError:
                    await st["qr"].recreate()
                    self.login_arm(uid, st)
                    continue
                except SessionPasswordNeededError:
                    st["stage"] = "pw"
                    self.login_note(uid, "QR scanned, 2-step password needed")
                    self.app(uid).wipe_input = True
                    await self.bot.send_message(uid, "🔒 QR accepted. This account has 2-step verification. Send its password. I delete your message right after.")
                    return
                self.login_note(uid, "QR scanned")
                done = await self.login_finish(uid, st)
                break
        except asyncio.CancelledError:
            raise
        except Exception as e:                                           # noqa: BLE001
            st.pop("qr_task", None)                                      # we are this task: don't cancel ourselves
            self.login_note(uid, "QR login failed: " + type(e).__name__)
            await self.login_drop(uid)
            done = f"❌ QR login failed: {html.escape(type(e).__name__)}. Send /session to start again.", _nav()
        if done:
            await self.bot.send_message(uid, done[0], parse_mode="HTML", reply_markup=to_markup(done[1]))

    async def login_step(self, uid: int, text: str) -> Reply:
        st = self.logins[uid]
        self.app(uid).wipe_input = True                                  # phone, code and password must not stay in the chat
        self.login_arm(uid, st)
        cancel = [[("Cancel", "sess:x")]]
        if st["stage"] == "qr":
            return "🔳 Waiting for the QR scan - nothing to type. Scan the QR I sent, or Cancel.", cancel
        try:
            from telethon.errors import (ApiIdInvalidError, FloodWaitError, PasswordHashInvalidError, PhoneCodeExpiredError,
                                         PhoneCodeInvalidError, PhoneNumberBannedError, PhoneNumberFloodError,
                                         PhoneNumberInvalidError, SendCodeUnavailableError, SessionPasswordNeededError)
        except ImportError:
            await self.login_drop(uid)
            return "Telethon is not installed on this bot.", _nav()
        try:
            if st["stage"] == "phone":
                phone = "+" + re.sub(r"\D", "", text)
                if len(phone) < 8:
                    return "That doesn't look like a phone number. Send it like <code>+15551234567</code>.", cancel
                if not await self.login_client(uid, st):
                    return "No Telegram api id/hash configured (SMSCENTER_TG_API_ID / SMSCENTER_TG_API_HASH).", _nav()
                sent = await st["client"].send_code_request(phone)
                st.update(phone=phone, hash=sent.phone_code_hash, stage="code")
                self.login_note(uid, f"code requested: type={type(sent.type).__name__} next={type(sent.next_type).__name__} timeout={getattr(sent, 'timeout', None)}")
                where, kb = self.code_where(sent, phone)
                return (where + "\nType it with <b>spaces or dashes</b> between the digits, e.g. <code>1 2 3 4 5</code> - "
                        "Telegram cancels codes that are sent to it as plain digits. I delete your message right after."), kb
            if st["stage"] == "code":
                code = re.sub(r"\D", "", text)
                if not code:
                    return "Send the code you received, digits with spaces or dashes (<code>1 2 3 4 5</code>).", cancel
                self.login_note(uid, f"code entered ({len(code)} digits)")
                try:
                    await st["client"].sign_in(phone=st["phone"], code=code, phone_code_hash=st["hash"])
                except SessionPasswordNeededError:
                    st["stage"] = "pw"
                    return "🔒 This account has 2-step verification. Send its password. I delete your message right after.", cancel
            else:
                await st["client"].sign_in(password=text)
            return await self.login_finish(uid, st)
        except PhoneCodeInvalidError:
            self.login_note(uid, "PhoneCodeInvalidError")
            return "❌ Wrong code. Type it again (with spaces), or Cancel.", cancel
        except PhoneCodeExpiredError:
            self.login_note(uid, "PhoneCodeExpiredError")
            await self.login_drop(uid)
            return "⌛ That code expired. Send /session to start again.", _nav()
        except PasswordHashInvalidError:
            self.login_note(uid, "PasswordHashInvalidError")
            return "❌ Wrong password. Send it again, or Cancel.", cancel
        except FloodWaitError as e:
            self.login_note(uid, f"FloodWaitError {e.seconds}s")
            await self.login_drop(uid)
            return f"⏳ Telegram says wait {e.seconds} seconds before trying again.", _nav()
        except PhoneNumberInvalidError:
            self.login_note(uid, "PhoneNumberInvalidError")
            await self.login_drop(uid)
            return "❌ Telegram says that phone number is invalid. Send /session to try again.", _nav()
        except SendCodeUnavailableError:
            self.login_note(uid, "SendCodeUnavailableError")
            await self.login_drop(uid)
            return "❌ Telegram has no way left to send a code to this number right now (all methods used or blocked). Wait a while, then send /session.", _nav()
        except PhoneNumberBannedError:
            self.login_note(uid, "PhoneNumberBannedError")
            await self.login_drop(uid)
            return "❌ Telegram says this phone number is banned.", _nav()
        except PhoneNumberFloodError:
            self.login_note(uid, "PhoneNumberFloodError")
            await self.login_drop(uid)
            return "❌ Telegram says this number asked for too many codes. Try again much later.", _nav()
        except ApiIdInvalidError:
            self.login_note(uid, "ApiIdInvalidError")
            await self.login_drop(uid)
            return "❌ The Telegram api id/hash is invalid (SMSCENTER_TG_API_ID / SMSCENTER_TG_API_HASH).", _nav()
        except Exception as e:                                           # noqa: BLE001  (never put e's text in the reply/log: it could echo input)
            await self.login_drop(uid)
            self.login_note(uid, "login failed: " + type(e).__name__)
            return f"❌ Login failed: {html.escape(type(e).__name__)}. Send /session to start again.", _nav()

    async def admin(self, cmd: str, arg: str) -> Reply:
        d = self.store.d
        if cmd == "/proxies":
            from tgbot_checker import proxies
            if arg.strip().lower() == "clear":
                d.pop("proxies", None)
                os.environ.pop("SMSCENTER_TG_PROXIES", None)
                self.store.save()
                await CHECKER.close()
                return "Proxies cleared. Telegram logins connect directly.", _nav()
            if arg.strip():
                prev = os.environ.get("SMSCENTER_TG_PROXIES")
                os.environ["SMSCENTER_TG_PROXIES"] = arg
                n = len(proxies())
                if not n:
                    if prev is None:
                        os.environ.pop("SMSCENTER_TG_PROXIES", None)
                    else:
                        os.environ["SMSCENTER_TG_PROXIES"] = prev
                    return "No proxy found. Send /proxies, a new line, then one <code>host:port:user:pass</code> per line.", _nav()
                self.store.set_secret("proxies", arg)
                await CHECKER.close()                                    # reconnects through the proxies
                return f"✅ Saved {n} proxies (your message is deleted). The next /session login and the checker use them.", _nav()
            n = len(proxies())
            return (f"{n} proxies are set." if n else "No proxies set.") + "\nSend /proxies, a new line, then one <code>host:port:user:pass</code> per line. <code>/proxies clear</code> removes them.", _nav()
        if cmd == "/access":
            v = arg.strip().lower()
            if v not in ("open", "closed"):
                return f"Access is <b>{d['access']}</b>.\nUsage: /access open|closed   (closed = only IDs you /allow)", _nav()
            d["access"] = v
            self.store.save()
            self.sync()
            return f"Access is now <b>{v}</b>.", _nav()
        if cmd in ("/allow", "/deny"):
            raw = arg.strip()
            if not raw.lstrip("-").isdigit():
                return f"Usage: {cmd} &lt;telegram user id&gt;", _nav()
            uid = int(raw)
            if cmd == "/allow":
                d["blocked"] = [x for x in d["blocked"] if x != uid]
                if uid not in d["allowed"]:
                    d["allowed"].append(uid)
                if str(uid) in d["users"]:
                    self.app(uid)                                        # resume their saved tracking
            else:
                d["allowed"] = [x for x in d["allowed"] if x != uid]
                if uid not in d["blocked"] and uid != d["admin"]:
                    d["blocked"].append(uid)
                self.sync()
            self.store.save()
            return (f"{uid} allowed." if cmd == "/allow" else f"{uid} blocked."), _nav()
        if cmd == "/session":
            return await self.login_start(d["admin"])
        lines = [f"Access: {d['access']}", f"Allowed IDs: {', '.join(map(str, d['allowed'])) or '-'}", f"Blocked IDs: {', '.join(map(str, d['blocked'])) or '-'}", ""]
        for k, prof in d["users"].items():
            n = sum(1 for t in prof.get("tracks", {}).values() if t.get("on"))
            lines.append(f"{k}{' (admin)' if k == str(d['admin']) else ''} · {n} tracking · key {'set' if prof.get('api_key') else 'not set'}")
        return "<b>Users</b>\n" + pre(lines), _nav()


# ── aiogram wiring ───────────────────────────────────────────────────────────────
def to_markup(kb: "KB | None"):
    if not kb:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    def btn(t: str, d: str):
        if d.startswith("copy:"):                                        # "copy to clipboard" button (Bot API copy_text; aiogram 3.10 has no field for it, extras pass through)
            return InlineKeyboardButton(text=t, copy_text={"text": d[5:]})
        return InlineKeyboardButton(text=t, callback_data=d)

    return InlineKeyboardMarkup(inline_keyboard=[[btn(t, d) for t, d in row] for row in kb])


# ── first-run setup: asks for everything once, checks it, saves it in the JSON, never asks again ──────────────
async def check_token(token: str) -> str | None:
    """@username if Telegram accepts the token, else None."""
    try:
        async with httpx.AsyncClient(timeout=15) as h:
            r = (await h.get(f"https://api.telegram.org/bot{token}/getMe")).json()
        return (r.get("result") or {}).get("username") if r.get("ok") else None
    except (httpx.HTTPError, ValueError):
        return None


async def check_key(key: str) -> float | None:
    """The account balance if SmsCenter accepts the key, else None."""
    c = SmsCenter(key)
    try:
        data = await c.get("/getBalance")
        return float(data.get("balance"))
    except (SmsCenterError, TypeError, ValueError):
        return None
    finally:
        await c.close()


def ask(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except EOFError:
        return None


async def setup(st: Store, force: bool = False) -> bool:
    """Ask for whatever is missing (or everything with --setup). Returns False if the bot can't start."""
    done = st.d.get("setup_done")                                        # once the wizard ran, skipped optional answers are not asked again
    need_token = force or not st.token
    need_admin = force or (st.d["admin"] is None and not done)
    if not (need_token or need_admin):
        return True
    if not sys.stdin or not sys.stdin.isatty():
        if not st.token:
            print("No bot token saved and no terminal to ask in. Run it once in a terminal.")
            return False
        return True                                                      # token exists; the admin can claim the bot with /start
    print(f"\nSetup - your answers are saved in {STATE_PATH} (the token is encrypted for your Windows account).")
    print("You will not be asked again. To change them later run:  python scripts/smscenter_price_bot.py --setup")
    print("Every user sets their OWN SmsCenter key inside the bot with /setkey.\n")
    if need_token:
        print("1) Telegram bot: create one with @BotFather and paste its token.")
        while True:
            tok = ask("   Bot token: ")
            if not tok:
                if st.token:
                    break                                                # keep the old one
                print("   A bot token is required.")
                if tok is None:
                    return False
                continue
            user = await check_token(tok)
            if user:
                st.set_secret("token", tok)
                print(f"   OK - @{user}")
                break
            print("   Telegram rejected that token. Copy it again from @BotFather (looks like 123456789:AA...).")
    if need_admin:
        print("2) Your Telegram user ID = the bot ADMIN (send /start to @userinfobot to see it).")
        print("   Press Enter to skip: then the first person who sends /start to your bot becomes the admin.")
        while True:
            oid = ask("   Your Telegram user ID: ")
            if not oid:
                break
            if oid.isdigit():
                st.d["admin"] = int(oid)
                if st.d.get("legacy"):
                    st.d["users"][oid] = st.d["legacy"]
                    st.d["legacy"] = None
                st.save()
                print("   OK")
                break
            print("   That should be a number like 123456789.")
    st.d["setup_done"] = True
    st.save()
    print("\nSetup saved.\n")
    return True


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for h in logging.getLogger().handlers:
        h.setFormatter(RedactFormatter(h.formatter._fmt if h.formatter else None))
    for n in ("httpx", "httpcore"):                      # their INFO lines carry full request URLs, key included
        logging.getLogger(n).setLevel(logging.WARNING)
    from aiogram import Bot, Dispatcher, F
    from aiogram.types import BotCommand, CallbackQuery, Message

    existed = os.path.exists(STATE_PATH)
    st = Store(STATE_PATH)
    if not await setup(st, force="--setup" in sys.argv):
        return 1

    bot = Bot(st.token)
    me = await bot.get_me()
    hub = Hub(st, bot)
    dp = Dispatcher()

    @dp.message(F.text, F.chat.type == "private")
    async def on_text(m: Message):
        res = await hub.handle(m.from_user.id, m.text)
        app_ = hub.apps.get(m.from_user.id)
        wipe = bool(app_ and app_.wipe_input)
        if app_:
            app_.wipe_input = False
        if m.text.startswith("/setkey") or wipe:
            try:
                await m.delete()                                          # don't leave the key in the chat
            except Exception:                                             # noqa: BLE001
                pass
        if res:
            await m.answer(res[0], parse_mode="HTML", disable_web_page_preview=True, reply_markup=to_markup(res[1]))

    @dp.callback_query()
    async def on_button(q: CallbackQuery):
        try:
            await q.answer()                                              # first: a slow screen must not make the tap "too old"; old taps from when the bot was off are just ignored
        except Exception:                                                 # noqa: BLE001
            pass
        res = await hub.ui(q.from_user.id, q.data or "")
        try:
            if res:
                await q.message.edit_text(res[0], parse_mode="HTML", disable_web_page_preview=True, reply_markup=to_markup(res[1]))
        except Exception as e:                                            # noqa: BLE001  ("message is not modified" etc.)
            if "not modified" not in str(e):
                log.warning("edit failed: %s", e)

    await bot.set_my_commands([BotCommand(command="menu", description="Main menu")])                # the only command in the Telegram menu: everything else is buttons
    print(f"Running as @{me.username}. " + ("Admin is set." if st.d["admin"] else "Open the bot and send /start to become the admin."))
    try:
        profs = [*st.d["users"].values(), *([st.d["legacy"]] if st.d.get("legacy") else [])]
        n_hunters = sum(len(p.get("hunters") or {}) for p in profs)
        run_id = os.environ.get("GITHUB_RUN_ID")
        await bot.send_message(st.d["admin"], f"Bot started. state.json {'loaded' if existed else '<b>not found</b> (fresh state)'}, hunters: <b>{n_hunters}</b>" + (f", run <code>{run_id}</code>" if run_id else ""), parse_mode="HTML")
    except Exception as e:                                                # noqa: BLE001
        log.warning("start notice failed: %s", e)
    hub.start_saved()                                                     # everybody's saved tracking resumes
    try:
        await dp.start_polling(bot)
    finally:
        hub.shutdown()
        await bot.session.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nStopped.")
