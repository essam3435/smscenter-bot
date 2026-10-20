"""Checks phone numbers by sending them to a Telegram checker bot (default @tgchecker3bababot) from a Telethon user session.

The bot takes up to 8 numbers per message (one per line) and answers with a "Check Completed" message that lists
  Fresh              -> clean (not registered, not banned)
  Registered / Used  -> registered
  Other              -> "<number> - <reason>", e.g. PHONE_NUMBER_BANNED -> banned

Used by numberhub_gui.py and number_validator_gui.py. Nothing here touches Qt.

IMPORTANT: a Telethon session file must only be used by ONE running program at a time (the auto-buyer included).
"""
from __future__ import annotations

import asyncio
import os
import random
import re
from typing import Callable

def proxies() -> list[tuple]:
    """Telethon proxy tuples from SMSCENTER_TG_PROXIES: one 'host:port:user:pass' per line (or ';'-separated).
    SMSCENTER_TG_PROXY_TYPE = socks5 (default) | http. Empty -> no proxy. Never log these."""
    kind = os.environ.get("SMSCENTER_TG_PROXY_TYPE", "").strip().lower() or "socks5"
    out = []
    for raw in re.split(r"[;\r\n]+", os.environ.get("SMSCENTER_TG_PROXIES", "")):
        p = raw.strip().split(":")
        if len(p) >= 4 and p[1].isdigit():
            out.append((kind, p[0], int(p[1]), True, p[2], ":".join(p[3:])))
    return out


async def connect_client(session, api_id: int, api_hash: str, note: Callable[[str], None] | None = None, shuffle: bool = False):
    """A connected TelegramClient: through the first working proxy of SMSCENTER_TG_PROXIES (in order, or in random order with shuffle), or direct when none is set."""
    from telethon import TelegramClient
    tries = list(enumerate(proxies(), 1)) or [(0, None)]               # (its number in the list, proxy)
    if shuffle:
        random.shuffle(tries)
    last = None
    for i, px in tries:
        client = TelegramClient(session, api_id, api_hash, proxy=px) if px else TelegramClient(session, api_id, api_hash)
        try:
            await asyncio.wait_for(client.connect(), 20)
            if note:
                note(f"connected via proxy #{i}" if px else "connected directly (no proxy)")
            return client
        except Exception as e:                                           # noqa: BLE001
            last = e
            if note:
                note(f"proxy #{i} failed: {type(e).__name__}" if px else f"direct connect failed: {type(e).__name__}")
            try:
                await client.disconnect()
            except Exception:                                            # noqa: BLE001
                pass
    raise last if last else RuntimeError("no connection")


BOT_DEFAULT = "tgchecker3bababot"
MAX_PER_REQUEST = 8
REPLY_TIMEOUT = 120.0
BUSY_MARK = "__BUSY__"
BUSY_WAIT = 4.0                 # the bot answers "you already have a checker request running" -> wait this long, ask again
BUSY_TRIES = 12

_NUM = re.compile(r"\+?\d[\d ]{5,18}\d")


def norm(phone: str) -> str:
    return "+" + re.sub(r"\D", "", str(phone))


def parse_report(text: str) -> dict[str, tuple[str, str]]:
    """'Check Completed' message -> {"+8180...": (verdict, detail)}; verdict is clean | registered | banned | unknown."""
    out: dict[str, tuple[str, str]] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        label = re.sub(r"[^A-Za-z /]", "", line).strip().lower()          # the line without emoji / digits / punctuation
        if ":" in line and re.search(r"(numbers|results)\s*:", line, re.I):  # summary counters ("Fresh Numbers: 0")
            continue
        if label in ("fresh",):
            section = "fresh"; continue
        if label.startswith("registered"):
            section = "registered"; continue
        if label in ("other", "others"):
            section = "other"; continue
        m = _NUM.search(line)
        if not m or not section:
            continue
        phone = norm(m.group(0))
        detail = line[m.end():].strip(" -—–:\t")
        if section == "fresh":
            out[phone] = ("clean", detail)
        elif section == "registered":
            out[phone] = ("registered", detail)
        else:
            out[phone] = ("banned" if "BANNED" in detail.upper() else "unknown", detail or "other result")
    return out


class BotChecker:
    """Holds one Telethon connection and sends batches of numbers to the checker bot."""

    def __init__(self, session: str, api_id: int, api_hash: str, bot: str = BOT_DEFAULT, log: Callable[[str], None] | None = None):
        self.session = re.sub(r"\.session$", "", session.strip())
        self.api_id, self.api_hash = int(api_id), api_hash
        self.bot = bot.strip().lstrip("@") or BOT_DEFAULT
        self.log = log or (lambda m: None)
        self.client = None
        self.entity = None

    async def open(self):
        from telethon import TelegramClient
        self.client = TelegramClient(self.session, self.api_id, self.api_hash)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise RuntimeError("the Telethon session is not logged in")
        self.entity = await self.client.get_entity(self.bot)

    async def close(self):
        if self.client is not None:
            await self.client.disconnect()

    async def roundtrip(self, phones: list[str]) -> str:
        """Send one message, return the bot's 'Check Completed' text. (The tests replace this.)"""
        from telethon import events
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        want = {norm(p) for p in phones}

        async def on_msg(ev):
            text = ev.raw_text or ""
            if "check completed" in text.lower():
                if want & set(parse_report(text)) and not fut.done():
                    fut.set_result(text)
            elif "already have a checker request running" in text.lower():
                if not fut.done():
                    fut.set_result(BUSY_MARK)                     # one request at a time per user: the caller waits and asks again
            elif text.strip():
                self.log(f"bot: {text.strip().splitlines()[0][:120]}")

        h_new = events.NewMessage(chats=self.entity)
        h_edit = events.MessageEdited(chats=self.entity)
        self.client.add_event_handler(on_msg, h_new)
        self.client.add_event_handler(on_msg, h_edit)
        try:
            await self.client.send_message(self.entity, "\n".join(norm(p) for p in phones))
            return await asyncio.wait_for(fut, REPLY_TIMEOUT)
        finally:
            self.client.remove_event_handler(on_msg, h_new)
            self.client.remove_event_handler(on_msg, h_edit)

    async def check(self, phones: list[str]) -> dict[str, tuple[str, str]]:
        """Up to MAX_PER_REQUEST numbers -> {number: (verdict, detail)}. Numbers the bot didn't mention are 'unknown'."""
        phones = [norm(p) for p in phones][:MAX_PER_REQUEST]
        try:
            for _ in range(BUSY_TRIES):
                text = await self.roundtrip(phones)
                if text != BUSY_MARK:
                    break
                await asyncio.sleep(BUSY_WAIT)
            else:
                return {p: ("unknown", "the checker bot stayed busy") for p in phones}
        except asyncio.TimeoutError:
            return {p: ("unknown", "no answer from the checker bot") for p in phones}
        rep = parse_report(text)
        return {p: rep.get(p, ("unknown", "not in the bot's answer")) for p in phones}
