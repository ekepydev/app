package com.urlvp

import android.net.Uri

/** Jednostavan blokator reklama (lista poznatih reklamnih domena), isto kao adblock.py. */
object AdBlock {
    @Volatile var on: Boolean = true
    @Volatile var hosts: List<String> = emptyList()  // dodatni dijelovi imena domene koje je upisao korisnik

    private val BLOCK = listOf(
        "doubleclick.net", "googlesyndication.com", "googleadservices.com", "adservice.google.com",
        "google-analytics.com", "googletagmanager.com", "googletagservices.com", "2mdn.net",
        "adnxs.com", "pubmatic.com", "rubiconproject.com", "openx.net", "criteo.com", "criteo.net",
        "taboola.com", "outbrain.com", "mgid.com", "revcontent.com", "amazon-adsystem.com",
        "adform.net", "smartadserver.com", "moatads.com", "scorecardresearch.com", "casalemedia.com",
        "popads.net", "popcash.net", "propellerads.com", "exoclick.com", "exosrv.com", "juicyads.com",
        "trafficjunky.com", "adsterra.com", "hilltopads.net", "clickadu.com", "onclickads.net",
        "adcash.com", "ad-maven.com", "admaven.com", "a-ads.com", "adsco.re", "tsyndicate.com",
        "syndication.exoclick.com", "realsrv.com", "bidvertiser.com", "yllix.com",
        "monetag.com", "pushnami.com", "richpush.co", "zeropark.com", "plugrush.com",
        "an.yandex.ru", "mc.yandex.ru", "ads.yahoo.com", "advertising.com", "adskeeper.com",
        "ero-advertising.com", "trafficstars.com", "adtng.com", "dtscout.com",
    )
    private val PATH = Regex("(/pagead/|/popunder|/popads|/adserver/|/prebid)", RegexOption.IGNORE_CASE)

    fun isAd(url: String, extra: List<String> = hosts): Boolean {
        val host = try { (Uri.parse(url).host ?: "").lowercase() } catch (e: Exception) { "" }
        if (host.isNotEmpty()) {
            for (d in BLOCK) if (host == d || host.endsWith(".$d")) return true
            for (e in extra) {
                val x = e.trim().lowercase()
                if (x.isNotEmpty() && host.contains(x)) return true
            }
        }
        val path = try { Uri.parse(url).path ?: "" } catch (e: Exception) { "" }
        return PATH.containsMatchIn(path)
    }
}
