"""Pravila po kojima se bira pravi link videa.

Dvije vrste pravila:
  * "url"    - uzorak linka, npr. https://cdn.primjer.com/videos/*/stream.m3u8?*
               Može se napraviti iz stvarnog primjera linka: dio koji se mijenja
               (token na kraju putanje ili u query-ju) automatski postaje *.
  * "domain" - dio imena domene (youtube, dailymotion...). Prihvata video linkove
               (m3u8, mp4... ili video content-type) sa te domene.
"""
import re
from urllib.parse import urlparse

TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_\-=.%~]{12,}$")
MEDIA_EXT = re.compile(r"\.(m3u8|mp4|webm|mpd|mkv|mov|ts|m4s)$", re.I)


def _looks_like_token(seg: str) -> bool:
    return bool(TOKEN_CHARS.match(seg)) and not MEDIA_EXT.search(seg)


def make_pattern(example: str) -> str:
    """Iz primjera linka napravi uzorak: token na kraju putanje i vrijednosti query-ja -> *."""
    p = urlparse(example.strip())
    segs = p.path.split("/")
    if segs and segs[-1] and _looks_like_token(segs[-1]):
        segs[-1] = "*"
    path = "/".join(segs)
    query = "?*" if p.query else ""
    frag = "#*" if p.fragment else ""
    return f"{p.scheme or 'https'}://{p.netloc}{path}{query}{frag}"


def rule_from_input(text: str):
    """Tekst koji korisnik upiše -> pravilo (dict) ili None."""
    t = (text or "").strip()
    if not t:
        return None
    if t.lower().startswith(("http://", "https://")):
        pattern = t if "*" in t else make_pattern(t)
        return {"kind": "url", "value": t, "pattern": pattern}
    return {"kind": "domain", "value": t, "pattern": t}


def _compile(rule):
    if rule.get("kind") != "url":
        return None
    pat = re.sub(r"^https?://", "", rule["pattern"].strip(), flags=re.I)
    optional_query = pat.endswith("?*")
    if optional_query:
        pat = pat[:-2]
    rx = "https?://" + ".*".join(re.escape(x) for x in pat.split("*"))
    if optional_query:
        rx += r"(\?.*)?$"
    return re.compile(rx, re.I)


class RuleSet:
    def __init__(self, rules):
        self.rules = [(r, _compile(r)) for r in (rules or [])]

    @property
    def active(self) -> bool:
        return bool(self.rules)

    def match(self, url: str, ctype: str, is_media: bool) -> bool:
        for rule, rx in self.rules:
            if rule.get("kind") == "domain":
                host = (urlparse(url).hostname or "").lower()
                if rule["value"].strip().lower() in host and is_media:
                    return True
            elif rx is not None and rx.match(url):
                return True
        return False
