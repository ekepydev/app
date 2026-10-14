"""Skuplja linkove na serije (svaka sezona je zasebna stranica /season/...) sa liste TV-Series.

Isto kao skripta za filmove, samo za https://ww8.123moviesfree.net/tv-series//N/ (oko 300 stranica).
Rezultat: sve_serije_ukupno_<broj>.txt pored skripte (jedan link po liniji) - to ubaciš u aplikaciju.

Dodatno: prekida se sama kad nekoliko stranica zaredom nema ništa novo, i upisuje fajl nakon svake stranice.
"""
import os
import random
import time

import cloudscraper
from bs4 import BeautifulSoup

BASE = "https://ww8.123moviesfree.net"
ZADNJA_STRANICA = 300
PRAZNIH_ZAREDOM_STOP = 5  # toliko stranica zaredom bez novih sezona = kraj liste


def pokreni_skener():
    scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "desktop": True})
    svi = set()
    folder = os.path.dirname(os.path.abspath(__file__))
    privremena = os.path.join(folder, "sve_serije_kompletna_baza.txt")
    praznih = 0

    print(f"START: skeniram serije od stranice 1 do {ZADNJA_STRANICA}...")
    for n in range(1, ZADNJA_STRANICA + 1):
        url = f"{BASE}/tv-series/" if n == 1 else f"{BASE}/tv-series//{n}/"
        print(f"Stranica {n}/{ZADNJA_STRANICA} | ukupno sezona: {len(svi)}")
        try:
            r = scraper.get(url, timeout=12)
            if r.status_code in (403, 429):
                print("Server usporava zahtjeve. Hladim sistem 20 sekundi...")
                time.sleep(20)
                r = scraper.get(url, timeout=12)
            if r.status_code != 200:
                print(f"Preskačem stranicu {n} - status {r.status_code}")
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            novih = 0
            # na stranici liste svaka serija je <a class="rounded poster" href=".../season/ime-sezone-ID/">
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/season/" in href:
                    link = href if href.startswith("http") else BASE + href
                    if link not in svi:
                        svi.add(link)
                        novih += 1

            if novih:
                praznih = 0
                with open(privremena, "w", encoding="utf-8") as f:
                    f.write("\n".join(sorted(svi)) + "\n")
            else:
                praznih += 1
                if praznih >= PRAZNIH_ZAREDOM_STOP:
                    print("Nekoliko stranica zaredom bez novih sezona - kraj liste.")
                    break
            time.sleep(random.uniform(0.6, 1.5))
        except Exception as e:  # noqa: BLE001
            print(f"Preskačem privremenu grešku na stranici {n}: {e}")
            time.sleep(3)

    if svi:
        konacna = os.path.join(folder, f"sve_serije_ukupno_{len(svi)}.txt")
        if os.path.exists(privremena):
            os.replace(privremena, konacna)
        print(f"\nKRAJ! Preuzeto {len(svi)} sezona -> {os.path.basename(konacna)}")
    else:
        print("Nije dohvaćeno ništa. Provjeri mrežu ili domenu.")


if __name__ == "__main__":
    pokreni_skener()
