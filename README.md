# URL Video Player

Čita URL-ove iz txt fajla (jedan po liniji), otvara stranicu, klikne play, uhvati link
videa koji odgovara tvom pravilu i pusti ga u prozoru aplikacije (HTML5 player, HLS preko hls.js).
Link ide kroz mali lokalni proxy koji šalje isti Referer/User-Agent kao stranica, a podržava i premotavanje.

## Katalog (postere, žanrovi, opisi)

Sučelje je katalog: mreža poster-kartica, odvojeno **Filmovi** i **Serije**, filter po **žanru**, pretraga, sortiranje
(redoslijed liste, naziv, ocjena, godina) i filter pogledano/nepogledano. Klik na karticu otvara detalje (poster, godina,
ocjena, žanrovi, opis); dugme **Gledaj** tek tada otvara stranicu, klikne play i hvata link. Sezone iste serije su
jedna kartica, a u detaljima se bira sezona.

- Podatke daje **TMDB** (IMDb nema besplatan javni API). Besplatan ključ: themoviedb.org → Settings → API.
  Upiši ga u **Postavke → TMDB** (radi i kratki „API Key“ i dugi „API Read Access Token“).
- Podaci za cijelu listu se povlače u pozadini (oko 6 zahtjeva u sekundi), prvo za kartice koje vidiš. Spremaju se u
  `meta_cache.json` pored `app.py`, pa se svaki naslov povlači samo jednom. Naslov koji nije nađen pokuša se ponovo nakon 14 dana.
- Naslov se traži po imenu iz URL-a (bez sezone i ID-a). Ako je poster pogrešan: detalji → **Ispravi…** → izaberi pravi naslov.
- Opis je na jeziku iz Postavki (zadano `hr-HR`); ako ga TMDB nema na tom jeziku, prikaže se engleski.
- **Full screen:** dugme ⛶ ili tipka **F** (ili dvoklik na video) stavlja prozor preko cijelog ekrana bez okvira; Esc izlazi iz full screena,
  pa iz playera. Traka iznad videa se sama sakrije nakon 3 s bez pokreta miša.
- Tipke u playeru: razmak = pauza, ←/→ = ±10 s, N/P = sljedeći/prethodni, M = bez zvuka. U katalogu strelice
  biraju kartice (radi i s daljinskim na TV-u).

## Instalacija

1. Instaliraj Python 3.9+. Na Windowsu treba i **WebView2 Runtime** (na Windows 10/11 je obično već instaliran).
2. U folderu aplikacije:

```
pip install -r requirements.txt
playwright install chromium
```

## Pokretanje

```
python app.py
```

Klikni "Učitaj txt…" i odaberi fajl. Dvoklik (ili Enter) na video pušta ga.

## Pravila za link ("Pravila…")

Da program ne uhvati pogrešan video (reklame i sl.), zadaš kako izgleda pravi link:

- **Primjer linka** — zalijepi jedan stvarni link, npr.
  `https://cdn.primjer.com/videos/abc/stream.m3u8?token=XYZ123`.
  Dio koji se mijenja (token na kraju putanje ili u query-ju) automatski postaje `*`:
  `https://cdn.primjer.com/videos/abc/stream.m3u8?*`
- **Ručni uzorak** — ako se mijenja još nešto (npr. ID videa u putanji), klikni
  "Uredi uzorak" i stavi `*` gdje treba: `https://cdn.primjer.com/videos/*/stream.m3u8?*`
- **Samo domena** — upiši npr. `youtube` ili `dailymotion`; prihvata video linkove sa
  domena koje sadrže taj tekst.
- Možeš imati više pravila odjednom (različite stranice u istoj listi).
- Bez pravila hvata se bilo koji video link.
- Ako stranica pusti video, a ništa ne odgovara pravilima, u "Pravila…" se pojavi lista
  linkova koje je program vidio — dvoklik na jedan ga ubacuje kao primjer.

## Duga lista koja raste

- Lista se prati: dopiši nove linkove u txt i sačuvaj — pojave se u aplikaciji u par sekundi
  (označeni sa "Novo"). Dugme "Osvježi" radi isto ručno.
- Aplikacija pamti listu, pravila, imena videa, šta je pogledano (✓) i zadnji video
  (`settings.json` pored `app.py`); metapodaci su u `meta_cache.json`.
- Polje "Pretraga" filtrira katalog po imenu ili linku.
- "Automatski sljedeći" prelazi na sljedeći video kad jedan završi; "Preskoči pogledane"
  preskače one sa ✓.

## Napomene

- Ime videa se prvo uzme iz URL-a, a zatim se zamijeni naslovom stranice kad se otvori.
- Pronađeni link se pamti 15 minuta (tokeni ističu); nakon toga ili ako player ne može pustiti
  link, stranica se automatski ponovo otvori i uzme svjež link (jednom po videu).
- Uzorak može sadržavati i dio iza `#` (npr. `watch/?v*#*`): i taj token se hvata i čuva u linku. Browser ga ne šalje u mrežnom zahtjevu, pa se traži i u adresama okvira (iframe) na stranici.
- Ako pronađeni link nije pravi video fajl nego stranica playera (npr. `.../watch/?v=TOKEN`), uz uključenu kvačicu „Izvuci stream (titlovi)“ aplikacija tu stranicu
  otvori u pozadini, klikne play i uhvati pravi stream (m3u8/mp4) pa ga pusti u običnom playeru (radi blokator,
  titlovi i automatski sljedeći). Samo ako i to ne uspije, stranica playera se prikaže u ugrađenom okviru
  (bez blokatora i titlova; „Otvori u browseru“ je tada alternativa). Bez te kvačice (default) stranica playera
  se odmah prikaže u okviru, kao u ranijoj verziji.
- DASH (.mpd) nije podržan u playeru.
- `settings.json` (lista, pravila, pogledano) nastaje pored `app.py`; kopiraj ga u ovaj folder da zadržiš postojeći napredak.
- hls.js se jednom preuzme u `web/vendor/hls.min.js` (već je priložen).
- Ako se link ne nađe, uključi "Prikaži browser" — neke stranice blokiraju headless browser.
- Videi zaštićeni DRM-om (Netflix i sl.) ne mogu se ovako pustiti.
- Klik na play traži poznate selektore (video.js, JW Player, Plyr…). Za stranicu sa
  nestandardnim dugmetom dodaj selektor u listu `PLAY_SELECTORS` u `resolver.py`.

## Blokiranje reklama

- Kvačica "Blokiraj reklame" (uključena po defaultu) radi na tri mjesta: u browseru koji traži link
  (reklamne domene se ne učitavaju, `window.open` pop-up/pop-under ne radi, reklamni video se ne može
  uhvatiti kao pravi), u lokalnom proxyju i u proxyju. Okvir (iframe) se ne sandboxuje jer mnogi playeri to odbijaju.
- Ugrađena je lista poznatih reklamnih mreža; dodatne domene upiši u "Pravila…" (dio imena, odvojeno sa `;`).
- Nije zamjena za uBlock. Reklame unutar ugrađenog okvira (iframe) se ne mogu filtrirati. Ako neki player ne radi, isključi kvačicu.

## Titlovi ("Titlovi…")

Titl se prikazuje u playeru (ne radi u ugrađenom okviru) i pamti se po videu u folderu `subs/`.

1. **Iz fajla** — dugme "Učitaj iz fajla" ili samo **povuci .srt/.vtt mišem u prozor**.
2. **Online (OpenSubtitles)** — ime filma se očisti od ID-a i dodataka, pa se traži. Treba besplatan API ključ
   (opensubtitles.com); za preuzimanje i korisnik/lozinka (spremaju se u `settings.json` kao običan tekst).
   Jezici se zadaju kao `hr,bs,sr,en`.
- Pomak titla ±0,5 s ako kasni ili žuri.
- Kodna stranica (UTF-8, cp1250, ISO-8859-2, cp852, UTF-16) prepoznaje se sama, a popravlja se i tekst koji je
  već pogrešno pretvoren (`Ä` umjesto `č`).

Izbornik "Auto titl" ispod playera: **online** traži i učita titl po imenu videa kad video krene
(samo u običnom playeru, ne u ugrađenom okviru).

## Pop-up prozori

Aplikacija ne dopušta da stranica unutar okvira otvori običan browser (Chrome i sl.): novi prozori se blokiraju
(Chromium oznaka `--block-new-web-contents` + zabrana `webbrowser.open`), a u traci se vidi "Blokirano pop-up
prozora: N" (broji se ispočetka za svaki video). Browser se otvara samo dugmetom „Otvori u browseru“.
Ako se Chrome ipak otvara, javi verziju pywebviewa (`pip show pywebview`).

## Windows exe

Na Windowsu dvoklik na `build_exe.bat` (treba Python i internet). Rezultat je folder `dist\URLVideoPlayer\` —
cijeli folder kopiraj gdje želiš i pokreni `URLVideoPlayer.exe`.

- Pri prvom pokretanju exe sam preuzme Chromium (~150 MB) u `browsers\` pored sebe.
- `settings.json` i folder `subs\` nastaju pored exe-a (ne u njemu), pa postojeći `settings.json` samo
  kopiraj u `dist\URLVideoPlayer\`.
- Ako Windows/antivirus upozori na nepoznatog izdavača, to je očekivano za exe koji nije potpisan.

## Titlovi i č, ć, š, ž, đ

Kodna stranica titla (UTF-8, cp1250, ISO-8859-2, cp852, UTF-16) se prepoznaje automatski po tome koja
daje najviše ispravnih slova č ć š ž đ. Stari spremljeni titlovi (bez oznake nove verzije) se ignorišu, pa se učitaju ponovo.

## Serije

- Stavka čiji link sadrži `/season/` je sezona serije. Iznad playera se pojavi traka s brojevima epizoda
  (zeleno = pogledano, plavo = trenutna). Klik na broj puni tu epizodu; „Prethodni/Sljedeći" i automatski prelazak
  idu epizodu po epizodu, pa tek onda na sljedeću stavku u listi.
- Program klikne play, pa na stranici klikne dugme epizode (`#ep-N` u listi `#eps-list`) i uhvati novi link
  po tvom pravilu (npr. `https://ployan.me/watch/?*#*`: `?v52` je epizoda 2, token iza `#` se mijenja).
- Zadnja epizoda i pogledane epizode pamte se u `settings.json`. Titlovi se pamte po epizodi
  i traže se s brojem sezone i epizode.
- Skupljanje linkova na sezone: `python tools/Skripta_za_serije.py` (stranice liste serija 1 do 300) pravi
  `sve_serije_ukupno_N.txt`. Za filmove ostaje `tools/Skripta_za_filmove.py`.
- Ako stranica nema `#eps-list` / `#ep-N`, javi mi njen HTML pa prilagodim selektore (`SWITCH_JS` u `resolver.py`).

## Android (telefon i TV)

Pogledaj `android/README_ANDROID.md`: isti kod sučelja, APK se pravi na GitHubu (Actions) bez Android Studija.

## Server vrati grešku / 404 pri hvatanju linka

Program prije klika na play čeka da se stranica učita i još nekoliko sekundi (zadano 6 s), a nakon uspješnog klika
ne klika ponovo prerano. Ako i dalje ne uhvati link ili player javi 404, povećaj „Čekanje prije klika na play"
u „Pravila…" (npr. na 8 ili 10). Isto vrijedi za serije (čeka se i prije izbora epizode).
Dobro je uključiti „Prikaži browser" i pogledati šta se desi nakon klika.
