"""Titlovi: parsiranje SRT/VTT i pretraga na OpenSubtitles.

Svi titlovi se u aplikaciji drže kao lista "cue-ova": {"s": početak_s, "e": kraj_s, "t": tekst}.
"""
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "subs")
OS_API = "https://api.opensubtitles.com/api/v1"
OS_UA = "URLVideoPlayer v1.0"


class SubError(RuntimeError):
    pass


# ======================================================================
# Parsiranje
# ======================================================================
TS = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)


def _sec(h, m, s, ms):
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


_BALKAN = "čćšžđČĆŠŽĐ"


def decode_bytes(b: bytes) -> str:
    """Pogodi kodnu stranicu titla. Balkanski titlovi su često cp1250, ISO-8859-2 ili cp852 (DOS)."""
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16", "replace")
    try:
        return b.decode("utf-8-sig")  # strogi UTF-8: ako prođe, to je to
    except UnicodeDecodeError:
        pass
    best, best_score = None, None
    for enc in ("cp1250", "iso-8859-2", "cp852"):
        try:
            t = b.decode(enc)
        except UnicodeDecodeError:
            continue
        good = sum(t.count(c) for c in _BALKAN)
        # znakovi kojih u pravom titlu nema (kontrolni, crtice za okvire...)
        bad = sum(1 for ch in t if "\x80" <= ch <= "\x9f" or "\u2500" <= ch <= "\u259f")
        score = good - 5 * bad
        if best_score is None or score > best_score:
            best, best_score = t, score
    return best if best is not None else b.decode("latin-1")


def fix_mojibake(t: str) -> str:
    """Popravi 'Ä\x8d', 'Å¡' i sl. (UTF-8 koji je prije toga pogrešno pročitan kao cp1250/cp1252)."""
    if not re.search("[\u00c3\u00c4\u00c5][\u0080-\u00bf\u0100-\u017f\u02c6-\u02dc\u2018-\u203a]", t):
        return t
    base = sum(t.count(c) for c in _BALKAN)
    for enc in ("cp1250", "cp1252", "latin-1"):
        try:
            fixed = t.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if sum(fixed.count(c) for c in _BALKAN) > base:
            return fixed
    return t


def parse_subs(text: str):
    """SRT ili WebVTT tekst -> lista cue-ova."""
    text = fix_mojibake(text.replace("\r\n", "\n").replace("\r", "\n"))
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip("\n").split("\n")
        for i, line in enumerate(lines):
            m = TS.search(line)
            if not m:
                continue
            g = m.groups()
            s, e = _sec(g[0], g[1], g[2], g[3]), _sec(g[4], g[5], g[6], g[7])
            body = "\n".join(lines[i + 1:])
            body = re.sub(r"<[^>]+>|\{\\[^}]*\}", "", body).strip()
            if body and e > s:
                cues.append({"s": round(s, 3), "e": round(e, 3), "t": body})
            break
    return cues


# ======================================================================
# Ime filma -> upit za pretragu
# ======================================================================
NOISE = re.compile(r"\b(full movie|watch online|online free|123movies)\b", re.I)


def split_season(name: str):
    """'Outer Banks - Season 4' -> ('Outer Banks', 4); bez sezone -> (ime, None)."""
    m = re.search(r"[\s\-–:]*\bSeason\s*(\d+)\b", name or "", re.I)
    if not m:
        return name, None
    return (name[:m.start()] + name[m.end():]).strip(" -–:"), int(m.group(1))


def clean_title(name: str):
    """'15 Cameras 1087' -> ('15 Cameras', None); 'Film 2019 1234 Full Movie' -> ('Film', 2019)."""
    t = NOISE.sub(" ", name or "")
    t = re.sub(r"[\[\(].*?[\]\)]", " ", t)
    toks = t.split()
    year = None
    while len(toks) > 1 and toks[-1].isdigit():
        n = toks[-1]
        if len(n) == 4 and 1900 <= int(n) <= 2099 and year is None:
            year = int(n)
            toks.pop()
        elif len(n) >= 4:
            toks.pop()  # ID iz URL-a
        else:
            break
    return " ".join(toks).strip(), year


# ======================================================================
# OpenSubtitles (REST API v1; treba besplatan API ključ sa opensubtitles.com)
# ======================================================================
def _http_json(url, headers=None, data=None, timeout=25):
    body = json.dumps(data).encode() if data is not None else None
    h = {"User-Agent": OS_UA, "Accept": "application/json"}
    if body is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("message", "")
        except Exception:
            detail = ""
        raise SubError(f"OpenSubtitles HTTP {e.code}. {detail}".strip())
    except Exception as e:  # noqa: BLE001
        raise SubError(f"OpenSubtitles nije dostupan: {e}")


def os_search(api_key, query, langs, year=None, season=None, episode=None):
    if not api_key:
        raise SubError("Upiši OpenSubtitles API ključ (besplatno na opensubtitles.com) u postavkama titlova.")
    params = {"query": query, "languages": langs or "en", "order_by": "download_count", "order_direction": "desc"}
    if year:
        params["year"] = year
    if season and episode:
        params["season_number"] = season
        params["episode_number"] = episode
    data = _http_json(f"{OS_API}/subtitles?{urllib.parse.urlencode(params)}", {"Api-Key": api_key})
    out = []
    for d in data.get("data", []):
        a = d.get("attributes", {})
        files = a.get("files") or []
        if not files:
            continue
        out.append({
            "file_id": files[0].get("file_id"),
            "release": a.get("release") or files[0].get("file_name") or "?",
            "lang": a.get("language") or "",
            "downloads": a.get("download_count") or 0,
        })
    return out[:25]


def os_download(api_key, user, password, file_id):
    headers = {"Api-Key": api_key}
    if user and password:
        login = _http_json(f"{OS_API}/login", headers, {"username": user, "password": password})
        if login.get("token"):
            headers["Authorization"] = "Bearer " + login["token"]
    data = _http_json(f"{OS_API}/download", headers, {"file_id": file_id})
    link = data.get("link")
    if not link:
        raise SubError(data.get("message") or "OpenSubtitles nije vratio link (dnevni limit?).")
    try:
        with urllib.request.urlopen(urllib.request.Request(link, headers={"User-Agent": OS_UA}), timeout=30) as r:
            raw = r.read()
    except Exception as e:  # noqa: BLE001
        raise SubError(f"Preuzimanje titla nije uspjelo: {e}")
    if raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    cues = parse_subs(decode_bytes(raw))
    if not cues:
        raise SubError("Titl je preuzet, ali se ne može pročitati.")
    return cues


# ======================================================================
# Keš (da se titl ne traži/generiše ponovo)
# ======================================================================
def _key(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def save_cues(url, source, cues):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, _key(url) + ".json"), "w", encoding="utf-8") as f:
            json.dump({"v": 2, "source": source, "cues": cues}, f, ensure_ascii=False)
    except OSError:
        pass


def load_cues(url):
    try:
        with open(os.path.join(CACHE_DIR, _key(url) + ".json"), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("v") != 2:
            return None  # spremljeno starim dekodiranjem: slova č ć š ž mogu biti pogrešna
        return d if d.get("cues") else None
    except Exception:
        return None
