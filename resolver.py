"""Otvara stranicu, klikne na play i hvata stvarni link videa iz mrežnih zahtjeva."""
import glob
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlparse, unquote

from playwright.sync_api import sync_playwright

from adblock import is_ad

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MEDIA_RE = re.compile(r"\.(m3u8|mp4|webm|mpd|mkv|mov)(\?|$)", re.I)
MEDIA_CTYPES = ("mpegurl", "video/mp4", "video/webm", "dash+xml")
SKIP_RE = re.compile(
    r"(doubleclick|googlesyndication|adservice|adsystem|/ads?/|analytics"
    r"|\.ts(\?|$)|\.m4s(\?|$)|\.aac(\?|$))",
    re.I,
)

# Najčešći selektori play dugmeta (video.js, JW Player, Plyr, YouTube, generički)
PLAY_SELECTORS = [
    "#play-now",
    "[id*='play' i][role='button']",
    "a[id*='play' i]",
    ".vjs-big-play-button",
    ".jw-icon-display",
    ".jw-display-icon-container",
    ".plyr__control--overlaid",
    ".ytp-large-play-button",
    "button[aria-label*='play' i]",
    "[title*='play' i]",
    ".play-button",
    ".play_button",
    ".btn-play",
    "#play",
    "#play-button",
    "[class*='play-btn' i]",
    "[class*='playbtn' i]",
    "[class*='play' i][role='button']",
    "button[class*='play' i]",
    "a[class*='play' i]",
    "div[class*='play' i]",
]

def ensure_browser(log=print):
    """Ako Chromium za Playwright nije preuzet (npr. prvo pokretanje exe-a), preuzmi ga."""
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if base and glob.glob(os.path.join(base, "chromium-*")):
        return
    if not getattr(sys, "frozen", False):
        return  # običan Python: korisnik je pokrenuo "playwright install chromium"
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        drv = compute_driver_executable()
        cmd = list(drv) if isinstance(drv, (tuple, list)) else [drv]
        log("Preuzimam Chromium (jednokratno, ~150 MB)…")
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}  # bez crnog prozora
        subprocess.run(cmd + ["install", "chromium"], env=get_driver_env(), check=True, **kw)
    except Exception as e:  # noqa: BLE001
        log(f"Chromium nije preuzet: {e}")


# Koliko sekundi čekati nakon što se pojavi video link koji NE odgovara pravilima
OTHERS_GRACE = 8


class NotFound(RuntimeError):
    """Link nije nađen; candidates = video linkovi koji su viđeni ali ne odgovaraju pravilima."""

    def __init__(self, msg, candidates=None):
        super().__init__(msg)
        self.candidates = candidates or []


def name_from_url(url: str) -> str:
    """Ime videa iz URL-a (zadnji dio putanje), npr. /watch/moj-film-2020 -> Moj Film 2020."""
    p = urlparse(url)
    parts = [s for s in p.path.split("/") if s]
    slug = unquote(parts[-1]) if parts else (p.hostname or url)
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
    slug = re.sub(r"[-_+]+", " ", slug).strip()
    return slug.title() if slug else (p.hostname or url)


def generic_match(u: str, ctype: str = "") -> bool:
    """Liči li link na video (po ekstenziji ili content-type), bez reklama i segmenata."""
    if SKIP_RE.search(u):
        return False
    return bool(MEDIA_RE.search(u)) or any(k in ctype for k in MEDIA_CTYPES)


def _try_click(page, extra=None) -> bool:
    """Pokuša kliknuti play u glavnoj stranici i svim iframeovima.

    extra: vlastiti CSS selektori (iz aplikacije) koji se probaju prvi.
    """
    selectors = list(extra or []) + PLAY_SELECTORS
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=1500, force=True)
                    return True
            except Exception:
                continue
        # direktno pokreni <video> ako postoji
        try:
            frame.evaluate(
                "document.querySelectorAll('video').forEach(v=>{v.muted=true;"
                "v.play().catch(()=>{})})"
            )
        except Exception:
            pass
    # zadnja opcija: klik u sredinu playera / iframea
    try:
        for sel in ("video", "iframe"):
            el = page.locator(sel).first
            if el.count():
                box = el.bounding_box()
                if box:
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    return True
    except Exception:
        pass
    return False


# Lista epizoda na stranici sezone: <div id="eps-list"> sa <button id="ep-1">, <button id="ep-2">...
SWITCH_JS = """(n) => { const b = document.querySelector('#ep-' + n); if (!b) return 'none';
  if (b.classList.contains('active')) return 'active'; b.click(); return 'clicked'; }"""
COUNT_JS = "() => document.querySelectorAll('#eps-list button').length"


def _switch_episode(page, n) -> str:
    """'clicked' | 'active' | 'none' (lista epizoda još nije tu)."""
    for frame in page.frames:
        try:
            st = frame.evaluate(SWITCH_JS, n)
            if st in ("clicked", "active"):
                return st
        except Exception:
            continue
    return "none"


def _episode_count(page) -> int:
    best = 0
    for frame in page.frames:
        try:
            best = max(best, int(frame.evaluate(COUNT_JS) or 0))
        except Exception:
            continue
    return best


def _video_src(page):
    for frame in page.frames:
        try:
            src = frame.evaluate(
                "(()=>{const v=document.querySelector('video');"
                "return v ? (v.currentSrc||v.src||'') : ''})()"
            )
            if src and src.startswith("http"):
                return src
        except Exception:
            continue
    return ""


def _pick(found):
    # HTML5 player ne može DASH (.mpd), pa mp4/webm imaju prednost nad njim
    for ext in ("m3u8", "mp4", "webm", "mkv", "mov", "mpd"):
        for u in found:
            if re.search(r"\." + ext + r"(\?|$)", u, re.I):
                return u
    return found[0] if found else ""


def resolve(url: str, rules=None, headless: bool = True, timeout: int = 40, log=print,
            click_selectors=None, adblock: bool = True, ad_hosts=None, referer=None, episode: int = 0,
            click_delay: float = 6.0) -> dict:
    """Vraća dict: title, media, referer, user_agent, others, episodes.

    click_delay: sekundi čekanja nakon učitavanja stranice prije klika na play (da player stigne da se pokrene).
    episode: za sezonu serije broj epizode (1..N); 0 = ne diraj izbor epizode (filmovi).

    rules: RuleSet. Ako ima pravila, hvata se samo link koji im odgovara; ako je
    prazan/None, hvata se bilo koji video link. Baca NotFound ako link nije nađen.
    """
    found = []
    others = []  # video linkovi koji ne odgovaraju pravilima
    first_other = {"t": None}
    ignore = set()  # linkovi prethodne epizode: ne računaju se nakon prebacivanja
    use_rules = rules is not None and rules.active

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        try:
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
            if adblock:
                # reklamne domene se ne učitavaju, a window.open (pop-up/pop-under) ne radi ništa
                ctx.add_init_script("window.open = function () { return null; };")

                def route_handler(route):
                    u = route.request.url
                    try:
                        blocked = is_ad(u, ad_hosts) and not (use_rules and rules.match(u, "", False))
                    except Exception:
                        blocked = False
                    if blocked:
                        return route.abort()
                    return route.continue_()

                ctx.route("**/*", route_handler)
            page = ctx.new_page()

            # zatvori pop-up reklame koje otvori klik
            def on_new_page(pg):
                if pg != page:
                    try:
                        pg.close()
                    except Exception:
                        pass

            ctx.on("page", on_new_page)

            def check(u, ctype=""):
                if u.startswith(("data:", "blob:")) or u in ignore:
                    return
                if adblock and is_ad(u, ad_hosts) and not (use_rules and rules.match(u, ctype, False)):
                    return  # reklama: ne računa se kao video
                gen = generic_match(u, ctype)
                if use_rules:
                    if rules.match(u, ctype, gen):
                        if u not in found:
                            found.append(u)
                            log(f"  nađen link po pravilu: {u[:110]}")
                    elif gen and u not in others:
                        others.append(u)
                        if first_other["t"] is None:
                            first_other["t"] = time.time()
                elif gen and u not in found:
                    found.append(u)
                    log(f"  nađen kandidat: {u[:110]}")

            def scan_frames():
                # adresa okvira (iframe) sadrži i dio iza #, koji se ne vidi u mrežnim zahtjevima
                for fr in page.frames:
                    try:
                        if fr.url.startswith("http"):
                            check(fr.url)
                    except Exception:
                        pass

            page.on("request", lambda r: check(r.url))
            page.on("framenavigated", lambda fr: check(fr.url) if fr.url.startswith("http") else None)
            page.on(
                "response",
                lambda r: check(r.url, (r.headers.get("content-type") or "").lower()),
            )

            log(f"Otvaram {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000, referer=referer)

            title = ""
            try:
                title = page.locator("meta[property='og:title']").first.get_attribute(
                    "content", timeout=1500
                ) or ""
            except Exception:
                pass
            if not title:
                try:
                    title = page.title()
                except Exception:
                    title = ""

            # pričekaj da se stranica i njene skripte učitaju prije klika (prebrz klik na play često ne uspije)
            try:
                page.wait_for_load_state("load", timeout=10000)
            except Exception:
                pass
            if click_delay > 0:
                log(f"  čekam {click_delay:g} s prije klika na play…")
                page.wait_for_timeout(int(click_delay * 1000))

            deadline = time.time() + timeout
            clicked_at = 0
            ok_click = False
            ready_at = time.time()  # epizoda se bira tek nakon što player ima vremena da se pokrene
            need_switch = bool(episode and episode > 1)  # epizoda 1 se sama učita nakon klika na play
            switched = False
            while time.time() < deadline and (not found or need_switch):
                # video je krenuo, ali ništa ne odgovara pravilima -> nema smisla čekati dalje
                if not need_switch and first_other["t"] and time.time() - first_other["t"] > OTHERS_GRACE:
                    break
                if need_switch and time.time() >= ready_at:
                    before = found + others  # linkovi stare epizode (događaj nove može stići već tokom klika)
                    st = _switch_episode(page, episode)
                    if st == "clicked":
                        log(f"  izabrana epizoda {episode}")
                        ignore.update(before)
                        found[:] = [u for u in found if u not in ignore]
                        others[:] = [u for u in others if u not in ignore]
                        first_other["t"] = None
                        need_switch = False
                        switched = True
                    elif st == "active":
                        need_switch = False
                # dok play nije kliknut pokušava se svake 3 s; nakon uspješnog klika se čeka duže da se ne pokreće iznova
                if not switched and time.time() - clicked_at > (6 if ok_click else 3):
                    if _try_click(page, click_selectors):
                        log("  kliknut play")
                        ok_click = True
                        ready_at = time.time() + click_delay
                    clicked_at = time.time()
                page.wait_for_timeout(500)
                if use_rules:
                    scan_frames()
                if not found:
                    src = _video_src(page)
                    if src:
                        check(src, "video/mp4")

            if found:
                page.wait_for_timeout(1200)  # pričekaj eventualni master playlist
                if use_rules:
                    scan_frames()

            if need_switch:
                raise NotFound(f"Lista epizoda nije pronađena (epizoda {episode}). Probaj uključiti prikaz browsera.", others)

            media = _pick(found)
            if not media:
                if use_rules and others:
                    raise NotFound(
                        "Nijedan link ne odgovara pravilima (pronađeni su drugi video linkovi).",
                        others,
                    )
                raise NotFound("Link videa nije pronađen (probaj uključiti prikaz browsera).", others)

            return {
                "title": title.strip(),
                "media": media,
                "referer": page.url,
                "user_agent": UA,
                "others": others,
                "episodes": _episode_count(page),
            }
        finally:
            browser.close()