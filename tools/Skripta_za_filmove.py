import time
import os
import random
from bs4 import BeautifulSoup
import cloudscraper

def pokreni_masovni_skener():
    # Initializing the scraper simulator to handle security walls
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True
        }
    )
    
    svi_linkovi = set()
    film_base_url = "https://ww8.123moviesfree.net"
    
    # Save directly in the directory where the script is executed
    trenutni_folder = os.path.dirname(os.path.abspath(__file__))
    privremena_putanja = os.path.join(trenutni_folder, "svi_filmovi_kompletna_baza.txt")
    
    # EXACT LIMIT ADJUSTED UP TO 700 PAGES
    ZADNJA_STRANICA = 700
    
    print("\nSTART: Skripta je pokrenuta! Pokrecem numericko skeniranje...")
    print(f"Sakupljam filmove od stranice 1 do {ZADNJA_STRANICA} koristeci zadani sablon...")
    
    for trenutna_stranica in range(1, ZADNJA_STRANICA + 1):
        # Page 1 uses the base listing directory, pages 2-700 inject the page number
        if trenutna_stranica == 1:
            url = "https://ww8.123moviesfree.net/movies/"
        else:
            url = f"https://ww8.123moviesfree.net/movies//{trenutna_stranica}/"
            
        print(f"Stranica {trenutna_stranica}/{ZADNJA_STRANICA} | Ukupno sakupljeno: {len(svi_linkovi)} filmova")
        
        try:
            # Request page data with a strict timeout limitation
            response = scraper.get(url, timeout=12)
            
            # Anti-spam protection rule: wait if the server responds with slow-down codes (403/429)
            if response.status_code == 403 or response.status_code == 429:
                print("Upozorenje: Server usporava zahteve. Hladim sistem 20 sekundi...")
                time.sleep(20)
                response = scraper.get(url, timeout=12)
            
            if response.status_code != 200:
                print(f"Preskacem stranicu {trenutna_stranica} - Server vratio status: {response.status_code}")
                continue
                
            soup = BeautifulSoup(response.text, 'html.parser')
            pronadjeno_na_stranici = 0
            
            # 1. Processing all valid movie element references found on the page layout
            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href']
                if "/movie/" in href:
                    puni_link = href if href.startswith('http') else film_base_url + href
                    if puni_link not in svi_linkovi:
                        svi_linkovi.add(puni_link)
                        pronadjeno_na_stranici += 1
            
            # Live storage update: Dump collected items directly to your file system after each page
            if pronadjeno_na_stranici > 0:
                with open(privremena_putanja, "w", encoding="utf-8") as f:
                    for link in sorted(svi_linkovi):
                        f.write(link + "\n")
            
            # Add a slight randomized execution latency to emulate real human browsing patterns
            time.sleep(random.uniform(0.6, 1.5))
            
        except Exception as e:
            print(f"Preskacem privremenu gresku na stranici {trenutna_stranica}: {e}")
            time.sleep(3)
            continue

    # Execution finalize summary output logic
    if svi_linkovi:
        konacna_putanja = os.path.join(trenutni_folder, f"svi_filmovi_ukupno_{len(svi_linkovi)}.txt")
        if os.path.exists(privremena_putanja):
            os.rename(privremena_putanja, konacna_putanja)
            
        print(f"\nKRAJ! Uspjesno preuzeto {len(svi_linkovi)} jedinstvenih filmova.")
        print(f"Datoteka se nalazi u vasem folderu: svi_filmovi_ukupno_{len(svi_linkovi)}.txt")
    else:
        print("Skripta nije uspela dohvatiti podatke. Provjerite mrezna ogranicenja ili domenu.")

if __name__ == "__main__":
    pokreni_masovni_skener()
