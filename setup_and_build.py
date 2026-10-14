#!/usr/bin/env python3
"""Sve u jednom: složi fajlove, provjeri/preuzme/ažurira sve što treba i (opcionalno) napravi exe.

  python setup_and_build.py              # složi + provjeri/ažuriraj + napravi exe
  python setup_and_build.py --no-build   # samo priprema (za pokretanje sa: python app.py)
  python setup_and_build.py --no-update  # ne ažuriraj ono što je već instalirano

Pravilo: ako nešto nedostaje -> preuzme se; ako već postoji -> pokuša se ažurirati
(ako ažuriranje ne uspije, ostaje postojeća verzija).
"""
import argparse
import glob
import importlib.metadata as md
import os
import re
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
HLS = os.path.join(WEB, "vendor", "hls.min.js")
HLS_URL = "https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"
NAME = "URLVideoPlayer"


def say(msg=""):
    print(msg, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, **kw).returncode == 0


def pkg_name(line):
    line = line.split("#")[0].strip()
    m = re.match(r"[A-Za-z0-9_.\-]+", line)
    return m.group(0) if m else ""


def read_reqs(fname):
    p = os.path.join(ROOT, fname)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [n for n in (pkg_name(l) for l in f) if n]


# ---------------------------------------------------------------- 1. fajlovi
def step_layout():
    say("[1/5] Raspored fajlova")
    os.makedirs(os.path.join(WEB, "vendor"), exist_ok=True)

    def move(srcs, dst, label):
        if os.path.exists(dst):
            say(f"  ok     {label}")
            return
        for s in srcs:
            sp = os.path.join(ROOT, s)
            if os.path.exists(sp):
                shutil.move(sp, dst)
                say(f"  prebačeno {s} -> {os.path.relpath(dst, ROOT)}")
                return
        say(f"  FALI   {label}")

    move(["index.html"], os.path.join(WEB, "index.html"), "web/index.html")
    move(["hls_min.js", "hls.min.js"], HLS, "web/vendor/hls.min.js (provjera ispod)")
    move(["android_shim.js"], os.path.join(WEB, "android_shim.js"), "web/android_shim.js")
    for need in ("app.py", "resolver.py", "rules.py", "adblock.py", "subs.py", "meta.py"):
        if not os.path.exists(os.path.join(ROOT, need)):
            say(f"  FALI   {need}")
            sys.exit(1)
    if not os.path.exists(os.path.join(WEB, "index.html")):
        say("  web/index.html ne postoji, aplikacija bi se otvorila prazna.")
        sys.exit(1)


# ---------------------------------------------------------------- 2. paketi
def step_packages(args):
    say("[2/5] Python paketi")
    names = read_reqs("requirements.txt")
    if not args.no_build:
        names.append("pyinstaller")
    for n in dict.fromkeys(names):
        try:
            ver = md.version(n)
        except md.PackageNotFoundError:
            ver = None
        if ver is None:
            say(f"  {n}: nije instaliran, preuzimam…")
            if not run([sys.executable, "-m", "pip", "install", n]):
                say(f"  GREŠKA: {n} se ne može instalirati.")
                sys.exit(1)
        elif args.no_update:
            say(f"  {n} {ver}: ok")
        else:
            say(f"  {n} {ver}: tražim noviju verziju…")
            if not run([sys.executable, "-m", "pip", "install", "-U", n]):
                say(f"  (ažuriranje nije uspjelo, ostaje {ver})")


# ---------------------------------------------------------------- 3. Chromium
def step_chromium(path=None):
    where = f" u {os.path.relpath(path, ROOT)}" if path else ""
    say(f"[3/5] Chromium za Playwright{where}")
    env = dict(os.environ)
    if path:
        env["PLAYWRIGHT_BROWSERS_PATH"] = path
    # "install" ništa ne radi ako je tačna verzija već tu, a skine je ako fali ili je zastarjela
    if not run([sys.executable, "-m", "playwright", "install", "chromium"], env=env):
        say("  UPOZORENJE: Chromium nije preuzet (nema interneta?). Aplikacija neće naći video linkove.")


# ---------------------------------------------------------------- 4. hls.js, ffmpeg
def step_extras(args):
    say("[4/5] hls.js")
    have = os.path.exists(HLS)
    if have and args.no_update:
        say("  hls.js: ok")
    else:
        try:
            with urllib.request.urlopen(HLS_URL, timeout=15) as r:
                data = r.read()
            if len(data) < 100_000:
                raise ValueError("preuzet fajl je premal")
            os.makedirs(os.path.dirname(HLS), exist_ok=True)
            with open(HLS, "wb") as f:
                f.write(data)
            say("  hls.js: " + ("ažuriran" if have else "preuzet"))
        except Exception as e:  # noqa: BLE001
            say(f"  hls.js: {'ostaje postojeći' if have else 'NIJE preuzet'} ({e})")
            if not have:
                sys.exit(1)


# ---------------------------------------------------------------- 5. exe
def step_build(args):
    say("[5/5] Pravljenje exe-a")
    sep = os.pathsep
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", NAME,
           "--add-data", f"web{sep}web", "--collect-all", "playwright", "--collect-all", "webview"]
    cmd.append("app.py")
    if not run(cmd):
        say("GREŠKA pri pravljenju exe-a.")
        sys.exit(1)
    out = os.path.join(ROOT, "dist", NAME)
    for extra in ("settings.json", "meta_cache.json"):
        src = os.path.join(ROOT, extra)
        if os.path.exists(src):
            shutil.copy2(src, out)
            say(f"  kopiran {extra}")
    # Chromium unutra, da exe radi odmah (i bez interneta)
    step_chromium(os.path.join(out, "browsers"))
    say(f"\nGotovo: {os.path.join(out, NAME)}.exe  (cijeli folder {os.path.relpath(out, ROOT)} je aplikacija)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-build", action="store_true", help="samo priprema, bez exe-a")
    ap.add_argument("--no-update", action="store_true", help="ne ažuriraj već instalirano")
    args = ap.parse_args()

    step_layout()
    step_packages(args)
    if args.no_build:
        step_chromium()
    else:
        say("[3/5] Chromium: preuzima se u fazi 5, uz exe")
    step_extras(args)
    if args.no_build:
        say("\nSpremno. Pokreni:  python app.py")
    else:
        step_build(args)


if __name__ == "__main__":
    main()
