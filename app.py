"""URL Video Player — HTML5 player u prozoru aplikacije (pywebview).

Lista linkova iz txt fajla -> otvori stranicu, klikni play -> uhvati link videa
(po pravilima koja zadaš) -> pusti u HTML5 <video> playeru.

Link videa ide kroz mali lokalni proxy (127.0.0.1) koji šalje isti Referer/User-Agent
kao stranica, jer CDN-ovi često odbijaju zahtjeve bez njih. Proxy podržava Range
(premotavanje) i prepisuje HLS (.m3u8) playliste da i segmenti idu kroz proxy.
"""
import base64
import http.server
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

# Pop-up reklame iz okvira traže novi prozor, a pywebview ga inače preda običnom browseru (Chrome...).
# Zato se webbrowser.open blokira; jedini dozvoljen put je dugme "Otvori u browseru" (_real_open).
_real_open = webbrowser.open
BLOCKED = {"n": 0, "last": ""}


def _guard_open(url, *args, **kwargs):
    BLOCKED["n"] += 1
    BLOCKED["last"] = str(url)
    print(f"Blokiran pop-up: {url}", flush=True)
    return False


webbrowser.open = _guard_open
webbrowser.open_new = _guard_open
webbrowser.open_new_tab = _guard_open
from collections import OrderedDict
from urllib.parse import parse_qs, quote, urljoin, urlparse

# WebView2 (Windows): dozvoli automatsko puštanje bez klika korisnika
os.environ.setdefault(
    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
    "--autoplay-policy=no-user-gesture-required --block-new-web-contents",  # bez pop-up prozora
)

# U exe-u Chromium živi pored aplikacije (browsers/); u običnom Pythonu se koristi standardni Playwright folder
if getattr(sys, "frozen", False):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(os.path.dirname(sys.executable), "browsers"))

try:
    import webview
except Exception:  # pywebview nije instaliran
    webview = None

import meta
import subs
from adblock import is_ad
from resolver import NotFound, ensure_browser, name_from_url
from resolver import resolve as resolve_page
from rules import RuleSet, rule_from_input

FROZEN = getattr(sys, "frozen", False)  # True kad radi kao exe (PyInstaller)
HERE = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))  # settings, subs, browsers
RES_DIR = getattr(sys, "_MEIPASS", HERE)  # ugrađeni fajlovi (web/)
WEB_DIR = os.path.join(RES_DIR, "web")
SETTINGS_PATH = os.path.join(HERE, "settings.json")
HLS_PATH = os.path.join(WEB_DIR, "vendor", "hls.min.js")
HLS_URL = "https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"
MEDIA_TTL = 15 * 60  # koliko dugo se pamti pronađeni link (tokeni ističu)
DEFAULT_CLICK_DELAY = 6.0  # sekundi čekanja prije klika na play
SERIES_RE = re.compile(r"/season/", re.I)  # stranica jedne sezone serije (ima listu epizoda)
MEDIA_LINK = re.compile(r"\.(m3u8|mp4|webm|mkv|mov|m4v|ogv|ogg|mpd)(\?|$)", re.I)
ADBLOCK = {"on": True, "hosts": []}  # dijeli se sa proxyjem
SUB_DEFAULTS = {
    "os_api_key": "", "os_user": "", "os_pass": "",
    "sub_langs": "hr,bs,sr",
    "auto_subs": "off",  # off | online
}
META_DEFAULTS = {"tmdb_key": "", "meta_lang": "hr-HR"}  # TMDB: poster, žanr, opis


# ======================================================================
# Postavke
# ======================================================================
def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("list_path", "")
    s.setdefault("lists", [])  # više txt listova odjednom (npr. filmovi + serije)
    if not s["lists"] and s["list_path"]:
        s["lists"] = [s["list_path"]]
    s.setdefault("rules", [])
    s.setdefault("click_selectors", [])
    s.setdefault("items", {})  # url -> {name, watched}
    s.setdefault("last", "")
    s.setdefault("autoplay", True)
    s.setdefault("skip_watched", False)
    s.setdefault("show_browser", False)
    s.setdefault("adblock", True)
    s.setdefault("extract_stream", False)
    s.setdefault("ad_hosts", [])
    for k, v in SUB_DEFAULTS.items():
        s.setdefault(k, v)
    for k, v in META_DEFAULTS.items():
        s.setdefault(k, v)
    if s["auto_subs"] != "off":
        s["auto_subs"] = "online"  # AI titlovi su izbačeni
    return s


# ======================================================================
# Proxy: sesije, prepisivanje HLS playlisti, HTTP handler
# ======================================================================
SESSIONS = OrderedDict()  # sid -> {referer, ua, origin}
_SESS_LOCK = threading.Lock()


def new_session(referer, ua):
    sid = secrets.token_urlsafe(9)
    pu = urlparse(referer or "")
    origin = f"{pu.scheme}://{pu.netloc}" if pu.scheme and pu.netloc else ""
    with _SESS_LOCK:
        SESSIONS[sid] = {"referer": referer or "", "ua": ua or "", "origin": origin}
        while len(SESSIONS) > 300:
            SESSIONS.popitem(last=False)
    return sid


def proxy_path(url, sid):
    return f"/p?s={sid}&u={quote(url, safe='')}"


def rewrite_m3u8(text, base, sid):
    """Svaki URI u playlisti (segmenti, podlistе, ključevi, init) usmjeri kroz proxy."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
        elif s.startswith("#"):
            line = re.sub(
                r'URI="([^"]+)"',
                lambda m: 'URI="%s"' % proxy_path(urljoin(base, m.group(1)), sid),
                line,
            )
            out.append(line)
        else:
            out.append(proxy_path(urljoin(base, s), sid))
    return "\n".join(out) + "\n"


MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8"}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # tiho
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/p":
                return self._proxy(parsed.query)
            rel = parsed.path.lstrip("/") or "index.html"
            full = os.path.normpath(os.path.join(WEB_DIR, rel))
            if not full.startswith(WEB_DIR + os.sep) or not os.path.isfile(full):
                return self._send(404, b"not found")
            ext = os.path.splitext(full)[1].lower()
            ctype = MIME.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _proxy(self, query):
        q = parse_qs(query)
        sid = (q.get("s") or [""])[0]
        target = (q.get("u") or [""])[0]
        sess = SESSIONS.get(sid)
        if sess is None or not target.lower().startswith(("http://", "https://")):
            return self._send(403, b"forbidden")
        if ADBLOCK["on"] and is_ad(target, ADBLOCK["hosts"]):
            return self._send(403, b"blocked (adblock)")

        headers = {
            "User-Agent": sess["ua"] or "Mozilla/5.0",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if sess["referer"]:
            headers["Referer"] = sess["referer"]
        if sess["origin"]:
            headers["Origin"] = sess["origin"]
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng

        try:
            resp = urllib.request.urlopen(urllib.request.Request(target, headers=headers), timeout=30)
        except urllib.error.HTTPError as e:
            resp = e  # HTTPError je ujedno i odgovor (status, headers, body)
        except Exception as e:  # noqa: BLE001
            return self._send(502, f"upstream error: {e}".encode())

        try:
            status = getattr(resp, "status", None) or resp.code
            ctype = resp.headers.get("Content-Type", "")
            final = resp.geturl()
            is_playlist = urlparse(final).path.lower().endswith(".m3u8") or "mpegurl" in ctype.lower()
            if is_playlist and status < 400:
                body = resp.read().decode("utf-8", "replace")
                data = rewrite_m3u8(body, final, sid).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_response(status)
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                v = resp.headers.get(h)
                if v:
                    self.send_header(h, v)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass  # player je prekinuo vezu (premotavanje, zamjena videa...)
        finally:
            try:
                resp.close()
            except Exception:
                pass


def start_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def ensure_hls():
    """Jednokratno preuzmi hls.js pored aplikacije; ako ne uspije, stranica ga učita sa CDN-a."""
    if os.path.exists(HLS_PATH):
        return
    try:
        with urllib.request.urlopen(HLS_URL, timeout=10) as r:
            data = r.read()
        if len(data) > 100_000:
            os.makedirs(os.path.dirname(HLS_PATH), exist_ok=True)
            with open(HLS_PATH, "wb") as f:
                f.write(data)
    except Exception as e:  # noqa: BLE001
        print(f"hls.js nije preuzet ({e}); koristit će se CDN.", flush=True)


# ======================================================================
# API koji poziva web sučelje (pywebview js_api)
# Samo metode bez "_" na početku su vidljive iz JavaScripta.
# ======================================================================
class Api:
    def __init__(self):
        self._lock = threading.RLock()
        self._s = load_settings()
        ADBLOCK["on"] = bool(self._s["adblock"])
        ADBLOCK["hosts"] = list(self._s["ad_hosts"])
        self._items = []  # {url, name, watched, new, media, media_ts, referer, ua, sid}
        self._by_url = {}
        self._version = 0
        self._list_mtimes = {}
        self._cands = []
        self._message = ""
        self._window = None
        self._fs = False
        self._meta = meta.Meta(lambda: self._s.get("tmdb_key", ""), lambda: self._s.get("meta_lang", "hr-HR"))
        if any(os.path.exists(p) for p in self._s["lists"]):
            self._load()

    # ---------- interno ----------
    def _bump(self):
        self._version += 1

    def _save(self):
        with self._lock:
            known = self._s["items"]
            for it in self._items:
                rec = {"name": it["name"], "watched": it["watched"]}
                if it["series"]:
                    rec.update(ep=it["ep"], eps=it["eps"], done=sorted(it["done"]))
                known[it["url"]] = rec
            try:
                with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                    json.dump(self._s, f, ensure_ascii=False, indent=1)
            except OSError:
                pass

    def _view(self):
        return [
            {"url": it["url"], "name": it["name"], "watched": it["watched"], "new": it["new"],
             "series": it["series"], "ep": it["ep"], "eps": it["eps"], "done": sorted(it["done"]),
             "mk": it["mk"], "base": it["base"], "season": it["season"], "year": it["year"]}
            for it in self._items
        ]

    def _state(self):
        with self._lock:
            lists = [{"path": p, "name": os.path.basename(p)} for p in self._s["lists"]]
            st = {
                "version": self._version,
                "list_name": ", ".join(x["name"] for x in lists),
                "lists": lists,
                "items": self._view(),
                "options": {
                    "autoplay": self._s["autoplay"],
                    "skip_watched": self._s["skip_watched"],
                    "show_browser": self._s["show_browser"],
                    "adblock": self._s["adblock"],
                    "extract_stream": self._s["extract_stream"],
                },
                "last": self._s["last"],
                "message": self._message,
            }
            self._message = ""
            return st

    def _load(self):
        lines, mtimes, problems = [], {}, []
        for path in list(self._s["lists"]):
            try:
                mtimes[path] = os.path.getmtime(path)
                with open(path, encoding="utf-8", errors="ignore") as f:
                    lines.extend(f.read().splitlines())
            except OSError as e:
                problems.append(f"{os.path.basename(path)}: {e}")
        if problems:
            self._message = "Ne mogu pročitati listu: " + "; ".join(problems)
        with self._lock:
            self._list_mtimes = mtimes
            urls, seen = [], set()
            for line in lines:
                line = line.strip()
                if line.startswith(("http://", "https://")) and line not in seen:
                    seen.add(line)
                    urls.append(line)

            known = self._s["items"]
            added = 0
            items = []
            for u in urls:
                it = self._by_url.get(u)
                if it is None:
                    saved = known.get(u, {})
                    it = {
                        "url": u,
                        "name": saved.get("name") or name_from_url(u),
                        "watched": bool(saved.get("watched")),
                        # "novo" = nije viđeno ranije (samo ako je aplikacija već pamtila neku listu)
                        "new": bool(known) and u not in known,
                        "series": bool(SERIES_RE.search(u)),
                        "ep": int(saved.get("ep") or 1),
                        "eps": int(saved.get("eps") or 0),
                        "done": set(int(x) for x in saved.get("done", [])),
                    }
                    series = it["series"]
                    base, year, season = meta.parse_name(name_from_url(u), series)  # po URL-u, ne po naslovu stranice
                    it.update(base=base, year=year, season=season, mk=meta.make_key(series, base, year))
                    if it["new"]:
                        added += 1
                items.append(it)
            self._items = items
            self._by_url = {it["url"]: it for it in items}
            if not problems:
                self._message = f"Lista: {len(items)} linkova" + (f" (+{added} novih)" if added else "")
            self._bump()
            self._save()
            seen_mk = {}
            for it in items:
                seen_mk.setdefault(it["mk"], (it["mk"], it["series"], it["base"], it["year"]))
            self._meta.set_items(list(seen_mk.values()))

    def _check_file(self):
        try:
            for p in self._s["lists"]:
                if os.path.exists(p) and os.path.getmtime(p) != self._list_mtimes.get(p):
                    self._load()
                    break
        except OSError:
            pass

    def _add_candidates(self, urls):
        for u in urls:
            if u in self._cands:
                self._cands.remove(u)
            self._cands.append(u)
        del self._cands[:-30]

    def _clear_media_cache(self):
        for it in self._items:
            it["cache"] = {}

    def _payload(self, it, ep, c):
        media = c["media"]
        sid = c.get("sid")
        if not sid or sid not in SESSIONS:
            sid = new_session(c.get("referer"), c.get("ua"))
            c["sid"] = sid
        low = media.lower()
        path = urlparse(media).path.lower()
        if path.endswith(".m3u8") or ".m3u8" in low:
            kind = "hls"
        elif path.endswith(".mpd"):
            kind = "dash"
        elif re.search(r"\.(mp4|m4v|webm|mkv|mov|ogv|ogg)(\?|$)", low):
            kind = "file"
        else:
            kind = "embed"  # npr. watch/?v=TOKEN stranica playera: ide u ugrađeni okvir
        return {
            "ok": True,
            "src": media if kind == "embed" else proxy_path(media, sid),
            "kind": kind,
            "media": media,
            "name": it["name"],
            "ep": ep,
            "episodes": it["eps"],
            "note": c.get("note", "") if kind == "embed" else "",
        }

    def _on_closed(self, *args):
        self._save()

    # ---------- lista ----------
    def init(self):
        return self._state()

    def poll(self, version):
        self._check_file()
        if version != self._version:
            return self._state()
        return {"version": self._version, "items": None}

    def choose_file(self):
        if self._window is None or webview is None:
            return None
        dlg = webview.FileDialog.OPEN if hasattr(webview, "FileDialog") else webview.OPEN_DIALOG
        res = self._window.create_file_dialog(
            dlg, allow_multiple=False, file_types=("Text files (*.txt)", "All files (*.*)")
        )
        if not res:
            return None
        path = res[0] if isinstance(res, (list, tuple)) else res
        with self._lock:  # lista se DODAJE na postojeće (filmovi + serije), ne zamjenjuje ih
            if path not in self._s["lists"]:
                self._s["lists"].append(path)
            self._s["list_path"] = path
        self._load()
        return self._state()

    def remove_list(self, path):
        with self._lock:
            if path in self._s["lists"]:
                self._s["lists"].remove(path)
            self._s["list_path"] = self._s["lists"][-1] if self._s["lists"] else ""
        self._load()
        return self._state()

    def refresh(self):
        self._load()
        return self._state()

    def set_option(self, key, value):
        if key in ("autoplay", "skip_watched", "show_browser", "adblock", "extract_stream"):
            self._s[key] = bool(value)
            if key == "adblock":
                ADBLOCK["on"] = bool(value)
            self._save()
        return True

    def set_last(self, url):
        self._s["last"] = url
        return True

    def mark_watched(self, url, ep=0):
        with self._lock:
            it = self._by_url.get(url)
            if it:
                it["new"] = False
                if it["series"] and ep:
                    it["done"].add(int(ep))
                    it["ep"] = int(ep)
                    if it["eps"] and len(it["done"]) >= it["eps"]:
                        it["watched"] = True
                else:
                    it["watched"] = True
                self._bump()
                self._save()
        return True

    def mark_unwatched(self, url, ep=0):
        with self._lock:
            it = self._by_url.get(url)
            if it:
                if it["series"] and ep:
                    it["done"].discard(int(ep))
                else:
                    it["done"].clear()
                it["watched"] = False
                self._bump()
                self._save()
        return True

    # ---------- traženje linka ----------
    def resolve(self, url, force=False, ep=0):
        BLOCKED["n"] = 0  # brojač blokiranih pop-upa kreće ispočetka za svaki video
        it = self._by_url.get(url)
        if it is None:
            return {"ok": False, "error": "Link nije u listi."}
        ep = int(ep or it["ep"] or 1) if it["series"] else 0
        cache = it.setdefault("cache", {})
        c = cache.get(ep)

        fresh = (not force) and c and time.time() - c.get("ts", 0) < MEDIA_TTL
        if not fresh:
            cache.pop(ep, None)
            holder = {}
            rules = RuleSet(self._s["rules"])
            headless = not self._s["show_browser"]
            selectors = list(self._s["click_selectors"])
            adblock = bool(self._s["adblock"])
            ad_hosts = list(self._s["ad_hosts"])
            extract = bool(self._s["extract_stream"])
            delay = self._click_delay()

            def work():
                log = lambda m: print(m, flush=True)  # noqa: E731
                try:
                    res = resolve_page(
                        url, rules=rules, headless=headless, click_selectors=selectors,
                        adblock=adblock, ad_hosts=ad_hosts, log=log, episode=ep, click_delay=delay,
                    )
                    if extract and not MEDIA_LINK.search(res["media"]):
                        # pronađeni link je stranica playera: otvori je i uhvati pravi stream iz nje
                        log("Link je stranica playera, tražim pravi video unutar nje…")
                        try:
                            r2 = resolve_page(
                                res["media"], rules=None, headless=headless, click_selectors=selectors,
                                adblock=adblock, ad_hosts=ad_hosts, referer=res["referer"], log=log, click_delay=delay,
                            )
                            res["page_link"] = res["media"]
                            res.update(media=r2["media"], referer=r2["referer"], user_agent=r2["user_agent"])
                            res["others"] = list(res.get("others", [])) + list(r2.get("others", []))
                        except Exception as e2:  # noqa: BLE001
                            res["stage2_error"] = str(e2)
                    holder["res"] = res
                except Exception as e:  # noqa: BLE001
                    holder["err"] = e

            t = threading.Thread(target=work, daemon=True)
            t.start()
            t.join()

            with self._lock:
                if "err" in holder:
                    err = holder["err"]
                    cands = getattr(err, "candidates", [])
                    if cands:
                        self._add_candidates(cands)
                    return {"ok": False, "error": str(err), "candidates": bool(cands)}
                res = holder["res"]
                c = {"media": res["media"], "ts": time.time(), "referer": res["referer"],
                     "ua": res["user_agent"], "sid": None, "note": res.get("stage2_error", "")}
                cache[ep] = c
                self._add_candidates(res.get("others", []))
                if it["series"]:
                    if res.get("episodes"):
                        it["eps"] = int(res["episodes"])
                    it["ep"] = ep
                if res["title"]:
                    it["name"] = res["title"]
                self._bump()
                self._save()
        else:
            it["ep"] = ep
        return self._payload(it, ep, c)

    # ---------- titlovi ----------
    def subs_settings(self):
        return {k: self._s[k] for k in SUB_DEFAULTS}

    def set_subs_settings(self, d):
        for k in SUB_DEFAULTS:
            if k in d:
                self._s[k] = str(d[k]).strip()
        self._save()
        return True

    def subs_cached(self, url):
        d = subs.load_cues(url)
        return {"cues": d["cues"], "source": d.get("source", "")} if d else None

    @staticmethod
    def _split_key(key):
        base, _, e = (key or "").partition("#e")
        return base, int(e) if e.isdigit() else 0

    def subs_search(self, url):
        base, ep = self._split_key(url)
        it = self._by_url.get(base)
        if it is None:
            return {"ok": False, "error": "Video nije u listi."}
        name, season = subs.split_season(it["name"]) if ep else (it["name"], None)
        query, year = subs.clean_title(name)
        try:
            res = subs.os_search(self._s["os_api_key"], query, self._s["sub_langs"], year,
                                 season=season, episode=ep or None)
        except subs.SubError as e:
            return {"ok": False, "error": str(e)}
        label = query + (f" ({year})" if year else "") + (f" S{season or '?'}E{ep}" if ep else "")
        return {"ok": True, "query": label, "results": res}

    def subs_download(self, url, file_id):
        try:
            cues = subs.os_download(self._s["os_api_key"], self._s["os_user"], self._s["os_pass"], file_id)
        except subs.SubError as e:
            return {"ok": False, "error": str(e)}
        subs.save_cues(url, "online", cues)
        return {"ok": True, "cues": cues}

    def subs_auto_online(self, url):
        """Traži titl po imenu videa i odmah preuzme najbolji (po redoslijedu jezika, pa broju preuzimanja)."""
        r = self.subs_search(url)
        if not r["ok"]:
            return r
        if not r["results"]:
            return {"ok": False, "error": "Nema rezultata za: " + r["query"]}
        langs = [x.strip().lower() for x in self._s["sub_langs"].split(",") if x.strip()]

        def rank(x):
            lang = (x.get("lang") or "").lower()
            return (langs.index(lang) if lang in langs else len(langs), -int(x.get("downloads") or 0))

        last = {"ok": False, "error": "Preuzimanje nije uspjelo."}
        for x in sorted(r["results"], key=rank)[:3]:
            d = self.subs_download(url, x["file_id"])
            if d["ok"]:
                return d
            last = d
        return last

    def subs_upload(self, url, data_b64):
        """Titl koji je sučelje pročitalo iz fajla (base64) -> cue-ovi. Kodna stranica se pogađa ovdje."""
        try:
            raw = base64.b64decode(data_b64 or "")
            cues = subs.parse_subs(subs.decode_bytes(raw))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"Titl se ne može pročitati: {e}"}
        if not cues:
            return {"ok": False, "error": "Fajl se ne može pročitati kao SRT/VTT."}
        subs.save_cues(url, "file", cues)
        return {"ok": True, "cues": cues}

    def open_external(self, url):
        if str(url).lower().startswith(("http://", "https://")):
            _real_open(url)
        return True

    def blocked_count(self):
        return BLOCKED["n"]

    # ---------- katalog (TMDB) ----------
    def meta_init(self):
        return {"genres": {str(k): v for k, v in meta.GENRES.items()}, "status": self._meta.status(),
                "has_key": bool(self._s.get("tmdb_key")), "base": meta.POSTER_BASE}

    def meta_since(self, ver):
        return self._meta.since(ver)

    def meta_status(self):
        return self._meta.status()

    def meta_want(self, mks):
        self._meta.want(mks or [])
        return True

    def meta_detail(self, mk):
        try:
            return self._meta.detail(mk)
        except Exception:  # noqa: BLE001
            return None

    def meta_search(self, query, series):
        try:
            return {"ok": True, "results": self._meta.search(str(query), bool(series))}
        except (meta.MetaAuthError, meta.MetaNetError) as e:
            return {"ok": False, "error": str(e)}

    def meta_pick(self, mk, tmdb_id, series):
        try:
            return {"ok": True, "meta": self._meta.pick(mk, tmdb_id, bool(series))}
        except (meta.MetaAuthError, meta.MetaNetError) as e:
            return {"ok": False, "error": str(e)}

    def get_tmdb_key(self):
        return {"key": self._s.get("tmdb_key", ""), "lang": self._s.get("meta_lang", "hr-HR")}

    def set_tmdb_key(self, key, lang="hr-HR"):
        self._s["tmdb_key"] = str(key or "").strip()
        self._s["meta_lang"] = str(lang or "hr-HR").strip() or "hr-HR"
        self._save()
        err = self._meta.check_key() if self._s["tmdb_key"] else ""
        self._meta.error = ""
        self._meta.want([])
        return {"ok": not err, "error": err}

    def set_fullscreen(self, on):
        """Pravi full screen prozora (bez okvira i trake zadataka)."""
        if self._window is None:
            return False
        on = bool(on)
        if on != self._fs:
            try:
                self._window.toggle_fullscreen()
                self._fs = on
            except Exception:  # noqa: BLE001
                return self._fs
        return self._fs

    # ---------- pravila ----------
    def get_rules(self):
        return {
            "rules": list(self._s["rules"]),
            "selectors": " ; ".join(self._s["click_selectors"]),
            "ad_hosts": " ; ".join(self._s["ad_hosts"]),
            "click_delay": self._click_delay(),
            "candidates": list(reversed(self._cands)),
        }

    def preview_rule(self, text):
        r = rule_from_input(text)
        if r is None:
            return {"kind": "", "pattern": ""}
        return {"kind": r["kind"], "pattern": r["pattern"] if r["kind"] == "url" else r["value"]}

    def add_rule(self, text):
        r = rule_from_input(text)
        with self._lock:
            if r and all(r["pattern"] != x["pattern"] for x in self._s["rules"]):
                self._s["rules"].append(r)
                self._clear_media_cache()  # stari linkovi možda ne odgovaraju novom pravilu
                self._save()
        return self.get_rules()

    def delete_rule(self, index):
        with self._lock:
            rules = self._s["rules"]
            if isinstance(index, int) and 0 <= index < len(rules):
                del rules[index]
                self._clear_media_cache()
                self._save()
        return self.get_rules()

    def set_ad_hosts(self, text):
        hosts = [x.strip().lower() for x in str(text).split(";") if x.strip()]
        self._s["ad_hosts"] = hosts
        ADBLOCK["hosts"] = list(hosts)
        self._save()
        return True

    def _click_delay(self):
        # "click_wait" se upisuje tek kad ga korisnik sam promijeni; inače važi zadana vrijednost
        try:
            return max(0.0, min(30.0, float(self._s["click_wait"])))
        except (KeyError, TypeError, ValueError):
            return DEFAULT_CLICK_DELAY

    def set_click_delay(self, value):
        try:
            self._s["click_wait"] = max(0.0, min(30.0, float(str(value).replace(",", "."))))
        except ValueError:
            return False
        self._clear_media_cache()
        self._save()
        return True

    def set_selectors(self, text):
        self._s["click_selectors"] = [x.strip() for x in str(text).split(";") if x.strip()]
        self._clear_media_cache()
        self._save()
        return True


def main():
    if webview is None:
        print("Nedostaje pywebview. Instaliraj ga:  pip install pywebview")
        sys.exit(1)
    ensure_hls()
    ensure_browser()
    port = start_server()
    api = Api()
    window = webview.create_window(
        "URL Video Player",
        f"http://127.0.0.1:{port}/index.html",
        js_api=api,
        width=1280,
        height=780,
        min_size=(900, 600),
    )
    api._window = window
    window.events.closed += api._on_closed
    webview.start()


if __name__ == "__main__":
    main()