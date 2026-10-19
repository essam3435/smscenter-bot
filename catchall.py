"""Catch-all e-mail helper for the SmsCenter bot: believable addresses on your own domain + reading what arrives in the Gmail the catch-all forwards to (IMAP, app password)."""
from __future__ import annotations

import asyncio
import email
import html
import imaplib
import random
import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

FIRST = ("james john robert michael william david richard joseph thomas charles daniel matthew anthony mark steven paul andrew joshua kevin brian george "
         "edward ryan jacob nicholas eric jonathan stephen larry justin scott brandon benjamin samuel frank gregory raymond patrick alexander jack dennis "
         "jerry tyler aaron henry adam nathan douglas peter zachary kyle walter ethan jeremy harold keith christian roger noah gerald carl terry sean "
         "mary patricia jennifer linda elizabeth barbara susan jessica sarah karen lisa nancy betty margaret sandra ashley kimberly emily donna michelle "
         "carol amanda melissa deborah stephanie rebecca laura sharon cynthia kathleen amy angela shirley anna brenda pamela nicole emma samantha "
         "katherine christine debra rachel catherine carolyn janet ruth maria heather diane olivia julie joyce victoria kelly christina lauren joan "
         "evelyn judith megan cheryl andrea hannah jacqueline martha gloria teresa ann sara madison frances kathryn janice jean abigail alice julia").split()
LAST = ("smith johnson williams brown jones garcia miller davis rodriguez martinez hernandez lopez gonzalez wilson anderson thomas taylor moore jackson "
        "martin lee perez thompson white harris sanchez clark ramirez lewis robinson walker young allen king wright scott torres nguyen hill flores green "
        "adams nelson baker hall rivera campbell mitchell carter roberts gomez phillips evans turner diaz parker cruz edwards collins reyes stewart morris "
        "morales murphy cook rogers gutierrez ortiz morgan cooper peterson bailey reed kelly howard ramos kim cox ward richardson watson brooks chavez "
        "wood james bennett gray mendoza ruiz hughes price alvarez castillo sanders patel myers long ross foster jimenez powell jenkins perry russell "
        "sullivan bell coleman butler henderson barnes gonzales fisher vasquez simmons graham murray ford hamilton owens harrison wallace").split()


def generate(domain: str, used: set[str] | frozenset[str] = frozenset()) -> tuple[str, str]:
    """(address, display name) like john.carter84@domain - never one already in `used`."""
    domain = domain.strip().lower().lstrip("@")
    for _ in range(200):
        f, l = random.choice(FIRST), random.choice(LAST)
        n = random.random()
        num = random.choice([str(random.randint(1, 99)), str(random.randint(1978, 2004))])
        local = random.choices(
            [f"{f}.{l}", f"{f}{l}", f"{f}_{l}", f"{f}.{l}{num}", f"{f}{l}{num}", f"{f[0]}{l}", f"{f[0]}.{l}{num}", f"{l}.{f}", f"{f}{num}", f"{f}.{l[0]}{num}"],
            weights=[22, 10, 6, 20, 12, 6, 6, 6, 6, 6])[0]
        addr = f"{local}@{domain}"
        if addr not in used:
            return addr, f"{f.title()} {l.title()}"
    raise RuntimeError("could not make a free address")


CODE_RES = (re.compile(r"(?:code|otp|pin|passcode|verification|verify|token)\D{0,40}?(\d[\d -]{2,9}\d)", re.I),
            re.compile(r"\b(\d{3}[ -]\d{3})\b"),
            re.compile(r"\b(\d{5,8})\b"))


def find_code(text: str) -> str | None:
    for rx in CODE_RES:
        m = rx.search(text)
        if m:
            c = re.sub(r"\D", "", m.group(1))
            if 4 <= len(c) <= 8:
                return c
    return None


def _dec(v) -> str:
    try:
        return str(make_header(decode_header(v or ""))).strip()
    except Exception:                                                    # noqa: BLE001
        return (v or "").strip()


def _body(msg) -> str:
    plain, rich = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html") or part.get_content_disposition() == "attachment":
            continue
        try:
            txt = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:                                                # noqa: BLE001
            continue
        if ct == "text/plain":
            plain += txt
        else:
            rich += txt
    if not plain and rich:
        plain = html.unescape(re.sub(r"<(script|style).*?</\1>|<[^>]+>", " ", rich, flags=re.S | re.I))
    return re.sub(r"\s+", " ", plain).strip()


def _fetch(gmail: str, password: str, addr: str, limit: int) -> list[dict]:
    out: list[dict] = []
    m = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=25)
    try:
        m.login(gmail, password)
        for folder in ("INBOX", "[Gmail]/Spam"):
            if m.select(f'"{folder}"', readonly=True)[0] != "OK":
                continue
            typ, data = m.search(None, "X-GM-RAW", f'"(to:{addr} OR deliveredto:{addr} OR cc:{addr})"')
            if typ != "OK":
                typ, data = m.search(None, "TO", f'"{addr}"')
            ids = (data[0] or b"").split()[-limit:]
            for i in reversed(ids):
                typ, parts = m.fetch(i, "(BODY.PEEK[])")
                if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                msg = email.message_from_bytes(parts[0][1])
                body = _body(msg)
                subj = _dec(msg.get("Subject"))
                try:
                    ts = parsedate_to_datetime(msg.get("Date")).timestamp()
                except Exception:                                        # noqa: BLE001
                    ts = 0.0
                out.append({"from": _dec(msg.get("From")), "subject": subj, "ts": ts, "body": body, "code": find_code(subj + " " + body), "spam": folder != "INBOX"})
    finally:
        try:
            m.logout()
        except Exception:                                                # noqa: BLE001
            pass
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:limit]


async def inbox(gmail: str, password: str, addr: str, limit: int = 4) -> list[dict]:
    return await asyncio.to_thread(_fetch, gmail, password, addr, limit)


def _login(gmail: str, password: str) -> None:
    m = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=20)
    try:
        m.login(gmail, password)
    finally:
        try:
            m.logout()
        except Exception:                                                # noqa: BLE001
            pass


async def test_login(gmail: str, password: str) -> str | None:
    """None = the Gmail accepted the app password, else a short reason."""
    try:
        await asyncio.to_thread(_login, gmail, password)
        return None
    except imaplib.IMAP4.error as e:
        t = str(e)
        return "Gmail rejected the login. Use a 16-letter App Password (Google account → Security → 2-Step Verification → App passwords), with IMAP enabled." if "AUTHENTICATIONFAILED" in t.upper() or "invalid" in t.lower() else t[:120]
    except Exception as e:                                               # noqa: BLE001
        return f"Could not reach Gmail ({type(e).__name__})."


ADDR_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.I)


class MailWatch:
    """Keeps one IMAP connection to the Gmail and reports each NEW message that was sent to an address on your domain (the first poll only marks 'now')."""

    def __init__(self):
        self.m: imaplib.IMAP4_SSL | None = None
        self.cfg: tuple | None = None
        self.last: int | None = None

    def close(self) -> None:
        if self.m is not None:
            try:
                self.m.logout()
            except Exception:                                            # noqa: BLE001
                pass
        self.m, self.cfg, self.last = None, None, None

    def poll(self, gmail: str, password: str, domain: str) -> list[dict]:
        if self.m is None or self.cfg != (gmail, password):
            self.close()
            m = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=25)
            m.login(gmail, password)
            self.m, self.cfg = m, (gmail, password)
        m = self.m
        m.select('"INBOX"', readonly=True)
        if self.last is None:
            typ, data = m.uid("SEARCH", None, "ALL")
            ids = (data[0] or b"").split() if typ == "OK" else []
            self.last = int(ids[-1]) if ids else 0
            return []
        typ, data = m.uid("SEARCH", None, f"UID {self.last + 1}:*")
        ids = sorted(int(x) for x in (data[0] or b"").split()) if typ == "OK" else []
        out: list[dict] = []
        for uid in (u for u in ids if u > self.last):
            self.last = max(self.last, uid)
            typ, parts = m.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1])
            heads = " ".join(_dec(msg.get(h)) for h in ("To", "Cc", "Delivered-To", "X-Original-To", "X-Forwarded-To"))
            hit = next((a.lower() for a in ADDR_RE.findall(heads) if a.lower().endswith("@" + domain.lower())), None)
            if not hit:
                continue
            body, subj = _body(msg), _dec(msg.get("Subject"))
            out.append({"to": hit, "from": _dec(msg.get("From")), "subject": subj, "body": body, "code": find_code(subj + " " + body), "ts": 0.0, "spam": False})
        return out
