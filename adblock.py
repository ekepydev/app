"""Jednostavan blokator reklama: lista poznatih reklamnih domena + nekoliko tipičnih putanja.

Nije potpun (nije zamjena za uBlock), ali skida većinu pop-up/pop-under i banner mreža.
Dodatne domene korisnik može upisati u "Pravila…" (dio imena domene, npr. adsterra).
"""
import re
from urllib.parse import urlparse

# Domena se blokira ako je jednaka ovome ili je njena poddomena.
BLOCK_HOSTS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com", "adservice.google.com",
    "google-analytics.com", "googletagmanager.com", "googletagservices.com", "2mdn.net",
    "adnxs.com", "pubmatic.com", "rubiconproject.com", "openx.net", "criteo.com", "criteo.net",
    "taboola.com", "outbrain.com", "mgid.com", "revcontent.com", "amazon-adsystem.com",
    "adform.net", "smartadserver.com", "moatads.com", "scorecardresearch.com", "casalemedia.com",
    "popads.net", "popcash.net", "propellerads.com", "exoclick.com", "exosrv.com", "juicyads.com",
    "trafficjunky.com", "adsterra.com", "hilltopads.net", "clickadu.com", "onclickads.net",
    "adcash.com", "ad-maven.com", "admaven.com", "a-ads.com", "adsco.re", "tsyndicate.com",
    "syndication.exoclick.com", "realsrv.com", "juicyads.com", "bidvertiser.com", "yllix.com",
    "monetag.com", "pushnami.com", "richpush.co", "zeropark.com", "plugrush.com",
    "an.yandex.ru", "mc.yandex.ru", "ads.yahoo.com", "advertising.com", "adskeeper.com",
    "ero-advertising.com", "trafficstars.com", "tsyndicate.com", "adtng.com", "dtscout.com",
)

PATH_RE = re.compile(r"(/pagead/|/popunder|/popads|/adserver/|/prebid)", re.I)


def host_blocked(host: str, extra=()) -> bool:
    host = (host or "").lower()
    if not host:
        return False
    for d in BLOCK_HOSTS:
        if host == d or host.endswith("." + d):
            return True
    for e in extra or ():
        e = e.strip().lower()
        if e and e in host:
            return True
    return False


def is_ad(url: str, extra=()) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return host_blocked(p.hostname or "", extra) or bool(PATH_RE.search(p.path or ""))
