"""Metapodaci o filmovima i serijama sa TMDB-a: poster, žanrovi, ocjena, opis.

IMDb nema besplatan javni API, pa se koristi TMDB (besplatan ključ: themoviedb.org -> Settings -> API).
Podaci se drže u meta_cache.json pored aplikacije, pa se svaki naslov povlači samo jednom.

Ključ ("mk") je naslov bez sezone/ID-a (npr. "t:outer banks"), pa sve sezone jedne serije dijele isti poster.
"""
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

from subs import clean_title, split_season

HERE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "meta_cache.json")
API = "https://api.themoviedb.org/3"
POSTER_BASE = "https://image.tmdb.org/t/p/w342"
MISS_RETRY = 14 * 86400  # naslov koji nije nađen pokušava se ponovo tek nakon 14 dana
DELAY = 0.15  # pauza između zahtjeva (~6/s; TMDB dozvoljava mnogo više)

GENRES = {
    28: "Akcija", 12: "Avantura", 16: "Animirani", 35: "Komedija", 80: "Krimi", 99: "Dokumentarni",
    18: "Drama", 10751: "Porodični", 14: "Fantastika", 36: "Povijesni", 27: "Horor", 10402: "Muzički",
    9648: "Misterija", 10749: "Romantika", 878: "Naučna fantastika", 10770: "TV film", 53: "Triler",
    10752: "Ratni", 37: "Vestern", 10762: "Dječiji", 10763: "Vijesti", 10764: "Reality",
    10766: "Sapunica", 10767: "Talk show",
}
# TV žanrovi koji spajaju dva filmska se razdvajaju, da jedan filter pokrije i filmove i serije
GENRE_MAP = {10759: [28, 12], 10765: [878, 14], 10768: [10752]}


class MetaAuthError(RuntimeError):
    pass


class MetaNetError(RuntimeError):
    pass


# ======================================================================
# Naslov iz imena -> ključ
# ======================================================================
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def parse_name(name: str, series: bool):
    """'Outer Banks Season 4 39274' -> ('Outer Banks', None, 4); 'Film 2019 1234' -> ('Film', 2019, None)."""
    base, season = split_season(name) if series else (name, None)
    title, year = clean_title(base)
    title = title or base or name
    return title.strip(), (None if series else year), season


def make_key(series: bool, title: str, year=None) -> str:
    return ("t:" if series else "m:") + norm(title) + (f":{year}" if year and not series else "")


# ======================================================================
# Meta
# ======================================================================
class Meta:
    def __init__(self, get_key, get_lang=lambda: "hr-HR"):
        self._get_key = get_key
        self._get_lang = get_lang
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        self._cache = {"v": 1, "r": {}, "o": {}, "ov": {}}  # r: rezultati, o: ručne ispravke, ov: opisi po jeziku
        self._targets = {}  # mk -> (series, title, year)
        self._order = []  # mk-ovi po redoslijedu liste
        self._sweep = 0
        self._prio = deque()
        self._busy = None
        self._log = []  # mk-ovi po redoslijedu upisa u keš; "verzija" je pozicija u ovoj listi
        self.error = ""
        self._dirty = 0
        self._last_save = time.time()
        self._load_cache()

    # ---------- keš ----------
    def _load_cache(self):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("v") == 1:
                self._cache = {"v": 1, "r": d.get("r", {}), "o": d.get("o", {}), "ov": d.get("ov", {})}
        except Exception:
            pass
        self._log = [k for k, e in self._cache["r"].items() if not e.get("miss")]

    def save(self):
        with self._lock:
            try:
                tmp = CACHE_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._cache, f, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp, CACHE_PATH)
            except OSError:
                pass
            self._dirty = 0
            self._last_save = time.time()

    def _store(self, mk, entry):
        with self._lock:
            self._cache["r"][mk] = entry
            if not entry.get("miss"):
                self._log.append(mk)
            self._dirty += 1
            if self._dirty >= 40 or time.time() - self._last_save > 15:
                self.save()

    # ---------- zadaci ----------
    @property
    def enabled(self) -> bool:
        return bool(self._get_key())

    def set_items(self, items):
        """items: lista (mk, series, title, year) po redoslijedu liste."""
        with self._lock:
            self._targets = {mk: (s, t, y) for mk, s, t, y in items}
            self._order = list(self._targets.keys())
            self._sweep = 0
        self._ensure_thread()
        self._wake.set()

    def want(self, mks):
        """Naslovi koje korisnik trenutno vidi: dolaze na početak reda."""
        with self._lock:
            for mk in reversed(list(mks)[:80]):
                if mk in self._targets and self._needs(mk) and mk not in self._prio and mk != self._busy:
                    self._prio.appendleft(mk)
        self._ensure_thread()
        self._wake.set()

    def _needs(self, mk) -> bool:
        e = self._cache["r"].get(mk)
        if e is None:
            return True
        return bool(e.get("miss")) and time.time() - e["miss"] > MISS_RETRY and mk not in self._cache["o"]

    def _next(self):
        with self._lock:
            while self._prio:
                mk = self._prio.popleft()
                if self._needs(mk):
                    return mk
            while self._sweep < len(self._order):
                mk = self._order[self._sweep]
                self._sweep += 1
                if self._needs(mk):
                    return mk
        return None

    def status(self):
        with self._lock:
            done = sum(1 for mk in self._order if mk in self._cache["r"])
            return {"enabled": self.enabled, "done": done, "total": len(self._order), "error": self.error}

    def _ensure_thread(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def _run(self):
        backoff = 5
        while True:
            if not self.enabled or self.error:
                self.save() if self._dirty else None
                self._wake.wait(3)
                self._wake.clear()
                if self.error and self.enabled:
                    self.error = ""  # ponovo pokušaj (npr. korisnik je promijenio ključ ili se vratila mreža)
                    time.sleep(30)
                continue
            mk = self._next()
            if mk is None:
                if self._dirty:
                    self.save()
                self._busy = None
                self._wake.wait(5)
                self._wake.clear()
                continue
            self._busy = mk
            try:
                self._store(mk, self._fetch(mk))
                backoff = 5
            except MetaAuthError as e:
                self.error = str(e)
                with self._lock:
                    self._prio.appendleft(mk)
            except MetaNetError:
                with self._lock:
                    self._prio.append(mk)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
            except Exception:  # noqa: BLE001 - neočekivan odgovor: ovaj naslov se preskače, ne ruši radnu nit
                self._store(mk, {"miss": int(time.time())})
            time.sleep(DELAY)

    # ---------- TMDB ----------
    def _get(self, path, params=None, timeout=20):
        key = (self._get_key() or "").strip()
        if not key:
            raise MetaAuthError("Upiši TMDB API ključ u postavkama.")
        q = dict(params or {})
        headers = {"Accept": "application/json", "User-Agent": "URLVideoPlayer/1.0"}
        if len(key) > 40:  # "API Read Access Token" (v4) umjesto kratkog v3 ključa
            headers["Authorization"] = "Bearer " + key
        else:
            q["api_key"] = key
        req = urllib.request.Request(f"{API}{path}?{urllib.parse.urlencode(q)}", headers=headers)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise MetaAuthError("TMDB je odbio ključ (401). Provjeri ključ u postavkama.")
                if e.code == 429:
                    time.sleep(min(30, int(e.headers.get("Retry-After", "2") or 2)))
                    continue
                if e.code == 404:
                    return {}
                raise MetaNetError(f"TMDB HTTP {e.code}")
            except Exception as e:  # noqa: BLE001
                raise MetaNetError(f"TMDB nije dostupan: {e}")
        raise MetaNetError("TMDB je preopterećen (429)")

    @staticmethod
    def _genres(ids):
        out = []
        for g in ids or []:
            for x in GENRE_MAP.get(g, [g]):
                if x in GENRES and x not in out:
                    out.append(x)
        return out

    @staticmethod
    def _entry(d, series):
        date = d.get("first_air_date" if series else "release_date") or ""
        genres = d.get("genre_ids")
        if genres is None:  # odgovor "details" ima listu objekata
            genres = [g.get("id") for g in d.get("genres", [])]
        return {
            "i": d.get("id"),
            "s": 1 if series else 0,
            "t": d.get("name" if series else "title") or "",
            "y": int(date[:4]) if date[:4].isdigit() else None,
            "p": d.get("poster_path") or "",
            "g": Meta._genres(genres),
            "r": round(float(d.get("vote_average") or 0), 1),
            "o": d.get("overview") or "",
        }

    @staticmethod
    def _score(res, series, title, year):
        want = norm(title)
        got = norm(res.get("name" if series else "title") or "")
        orig = norm(res.get("original_name" if series else "original_title") or "")
        s = 0
        if want and want in (got, orig):
            s += 3
        elif want and (want in got or got in want) and got:
            s += 1
        date = (res.get("first_air_date" if series else "release_date") or "")[:4]
        if year and date == str(year):
            s += 2
        return s

    def _search(self, series, title, year):
        kind = "tv" if series else "movie"
        tries = [year, None] if year and not series else [None]
        for y in tries:
            p = {"query": title, "include_adult": "false", "language": "en-US"}
            if y:
                p["year"] = y
            results = (self._get(f"/search/{kind}", p) or {}).get("results") or []
            if results:
                # sortirano po relevantnosti; bolji pogodak po naslovu/godini ima prednost, a izjednačeno ostaje prvi
                best = max(enumerate(results[:8]), key=lambda t: (self._score(t[1], series, title, year), -t[0]))[1]
                return best
        return None

    def _fetch(self, mk):
        with self._lock:
            series, title, year = self._targets[mk]
            ov = self._cache["o"].get(mk)
        if ov:
            d = self._get(f"/{'tv' if ov['s'] else 'movie'}/{ov['i']}", {"language": "en-US"})
            return self._entry(d, bool(ov["s"])) if d else {"miss": int(time.time())}
        res = self._search(series, title, year)
        if res is None:
            return {"miss": int(time.time())}
        return self._entry(res, series)

    # ---------- za sučelje ----------
    @staticmethod
    def compact(e):
        return {"i": e.get("i"), "t": e.get("t"), "y": e.get("y"), "p": e.get("p"), "g": e.get("g"), "r": e.get("r")}

    def since(self, ver, limit=1500):
        """Novi/izmijenjeni zapisi od 'ver'. Vraća {ver, more, base, data{mk: kompaktan zapis}}."""
        with self._lock:
            ver = max(0, int(ver or 0))
            chunk = self._log[ver:ver + limit]
            data = {}
            for mk in chunk:
                e = self._cache["r"].get(mk)
                if e and not e.get("miss"):
                    data[mk] = self.compact(e)
            new = ver + len(chunk)
            return {"ver": new, "more": new < len(self._log), "base": POSTER_BASE, "data": data}

    def detail(self, mk):
        """Puni zapis s opisom (na jeziku iz postavki ako ga TMDB ima, inače engleski)."""
        with self._lock:
            e = self._cache["r"].get(mk)
            if not e or e.get("miss"):
                return None
            e = dict(e)
        lang = self._get_lang() or "en-US"
        if lang != "en-US" and e.get("i") and self.enabled:
            with self._lock:
                text = self._cache["ov"].get(mk, {}).get(lang)
            if text is None:
                try:
                    d = self._get(f"/{'tv' if e.get('s') else 'movie'}/{e['i']}", {"language": lang})
                    text = (d or {}).get("overview") or ""
                except (MetaAuthError, MetaNetError):
                    text = None
                if text is not None:
                    with self._lock:
                        self._cache["ov"].setdefault(mk, {})[lang] = text
                        self._dirty += 1
            if text:
                e["o"] = text
        out = self.compact(e)
        out["o"] = e.get("o", "")
        out["base"] = POSTER_BASE
        return out

    def search(self, query, series):
        res = (self._get(f"/search/{'tv' if series else 'movie'}", {"query": query, "include_adult": "false",
                                                                     "language": "en-US"}) or {}).get("results") or []
        return [dict(self.compact(self._entry(r, series)), o=(r.get("overview") or "")[:160]) for r in res[:12]]

    def pick(self, mk, tmdb_id, series):
        """Ručna ispravka: korisnik bira pravi naslov za ovaj ključ."""
        d = self._get(f"/{'tv' if series else 'movie'}/{int(tmdb_id)}", {"language": "en-US"})
        if not d:
            raise MetaNetError("Naslov nije nađen na TMDB-u.")
        with self._lock:
            self._cache["o"][mk] = {"i": int(tmdb_id), "s": 1 if series else 0}
            self._cache["ov"].pop(mk, None)
        self._store(mk, self._entry(d, series))
        self.save()
        return self.detail(mk)

    def check_key(self):
        """Vrati '' ako ključ radi, inače poruku o grešci."""
        try:
            self._get("/configuration")
            self.error = ""
            return ""
        except MetaAuthError as e:
            return str(e)
        except MetaNetError as e:
            return str(e)
