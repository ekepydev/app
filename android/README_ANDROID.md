# Android (telefon + Android TV)

Ista aplikacija kao na desktopu: isto web sučelje (`../web`), a Python dio je prepisan u Kotlin
(skriveni WebView klikne play i hvata link, lokalni proxy šalje Referer, titlovi, pravila, blokator, serije).
Jedan APK radi na telefonu i na Android TV-u.

## Kako dobiti APK (bez Android Studija)

1. Na github.com napravi **novi repozitorij** (može privatni).
2. Priloži cijeli sadržaj ovog foldera (`web/`, `android/`, `.github/`, ... ) u repozitorij
   (GitHub: *Add file → Upload files*, povuci sve fajlove i foldere). `settings.json` se NE šalje (`.gitignore`).
3. Otvori karticu **Actions → Build APK → Run workflow** (ili se pokrene sam nakon prvog uploada).
4. Za 3-6 minuta: klikni na završeni run → dolje **Artifacts → URLVideoPlayer-apk** → skini zip → u njemu je `app-debug.apk`.
5. Instaliraj APK:
   - **Telefon:** prebaci APK, dozvoli „instalaciju iz nepoznatih izvora", otvori ga.
   - **Android TV:** instaliraj „Send Files to TV" (ili Downloader) i prebaci APK, ili preko `adb install app-debug.apk`.

Ako build javi grešku, kopiraj posljednjih 30 redova loga iz Actions i pošalji — ovaj kod nisam mogao
kompajlirati u svom okruženju, pa je moguće da treba sitna ispravka.

## Korištenje

- **Katalog:** isti kao na desktopu (postere, žanrovi, opisi s TMDB-a; TMDB ključ upiši u „Postavke“). Tipka nazad zatvara player/detalje, a tek onda aplikaciju.
- **Full screen:** dugme ⛶ u playeru sakriva statusnu i navigacijsku traku.
- **Lista:** „Učitaj txt…" otvara birač fajlova. Ako TV nema birač, otvori se polje za **link na txt listu**
  (npr. raw link s GitHuba/Dropboxa) i lista se čita s tog linka (pritisni „Osvježi" za novu verziju).
- **Dodir / OK na daljinskom** pušta video, strelice gore/dolje biraju iz liste, lijevo/desno prelaze na dugmad.
- **Serije:** stavka s `/season/` u linku dobije traku epizoda; automatski prelazi na sljedeću epizodu.
- Titl iz fajla: dugme „Učitaj iz fajla…" u prozoru Titlovi (kodna stranica se pogađa sama).

## Razlike u odnosu na desktop

- Nema opcije „Prikaži browser" (hvatanje linka radi u skrivenom WebView-u).
- Link se prepoznaje po ekstenziji u adresi (m3u8/mp4/webm), a ne i po content-type zaglavlju.
- `settings.json` i `meta_cache.json` s desktopa se ne uvoze automatski (pravila se unose ponovo u „Pravila…").
- DASH (.mpd) i DRM nisu podržani, isto kao na desktopu.
