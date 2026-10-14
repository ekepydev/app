package com.urlvp

import android.content.Context
import android.graphics.Bitmap
import android.os.Handler
import android.os.Looper
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import org.json.JSONArray
import org.json.JSONObject
import org.json.JSONTokener
import java.io.ByteArrayInputStream
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

class NotFound(msg: String, val candidates: List<String> = emptyList()) : RuntimeException(msg)

data class Resolved(
    val title: String, val media: String, val referer: String, val ua: String,
    val others: List<String>, val episodes: Int,
)

/**
 * Otvori stranicu u skrivenom WebView-u, klikni play (i izaberi epizodu) i uhvati pravi link videa
 * (isto kao resolver.py, samo što umjesto Playwrighta koristi Android WebView).
 * Poziva se iz radne niti (blokira dok ne nađe link ili ne istekne vrijeme).
 */
class Resolver(context: Context) {
    private val ctx = context.applicationContext
    private val main = Handler(Looper.getMainLooper())

    companion object {
        const val UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        private val MEDIA_RE = Regex("\\.(m3u8|mp4|webm|mpd|mkv|mov)(\\?|\$)", RegexOption.IGNORE_CASE)
        private val SKIP_RE = Regex("(doubleclick|googlesyndication|adservice|adsystem|/ads?/|analytics|\\.ts(\\?|\$)|\\.m4s(\\?|\$)|\\.aac(\\?|\$))", RegexOption.IGNORE_CASE)
        private const val OTHERS_GRACE_MS = 8000L

        val PLAY_SELECTORS = listOf(
            "#play-now", "[id*='play' i][role='button']", "a[id*='play' i]", ".vjs-big-play-button",
            ".jw-icon-display", ".jw-display-icon-container", ".plyr__control--overlaid", ".ytp-large-play-button",
            "button[aria-label*='play' i]", "[title*='play' i]", ".play-button", ".play_button", ".btn-play",
            "#play", "#play-button", "[class*='play-btn' i]", "[class*='playbtn' i]",
            "[class*='play' i][role='button']", "button[class*='play' i]", "a[class*='play' i]", "div[class*='play' i]",
        )

        fun genericMatch(u: String): Boolean = !SKIP_RE.containsMatchIn(u) && MEDIA_RE.containsMatchIn(u)

        fun pick(found: List<String>): String {
            for (ext in listOf("m3u8", "mp4", "webm", "mkv", "mov", "mpd")) {
                val rx = Regex("\\." + ext + "(\\?|\$)", RegexOption.IGNORE_CASE)
                for (u in found) if (rx.containsMatchIn(u)) return u
            }
            return found.firstOrNull() ?: ""
        }

        private const val SWITCH_JS =
            "(function(n){var b=document.querySelector('#ep-'+n);if(!b)return 'none';" +
                "if(b.classList.contains('active'))return 'active';b.click();return 'clicked';})(%d)"
        private const val COUNT_JS = "document.querySelectorAll('#eps-list button').length"
        private const val SCAN_JS =
            "(function(){try{var o={iframes:[],video:''};" +
                "document.querySelectorAll('iframe').forEach(function(f){if(f.src)o.iframes.push(f.src);});" +
                "var v=document.querySelector('video');if(v)o.video=v.currentSrc||v.src||'';" +
                "return JSON.stringify(o);}catch(e){return '{}';}})()"
        private const val CLICK_JS =
            "(function(sels){var done=false;" +
                "for(var i=0;i<sels.length&&!done;i++){try{var el=document.querySelector(sels[i]);" +
                "if(el&&el.getClientRects().length>0){el.click();done=true;}}catch(e){}}" +
                "document.querySelectorAll('video').forEach(function(v){v.muted=true;try{v.play();}catch(e){}});" +
                "return done?'clicked':'';})(%s)"
        private const val TITLE_JS =
            "(function(){var m=document.querySelector(\"meta[property='og:title']\");" +
                "return (m&&m.content)?m.content:document.title;})()"
    }

    private fun <T> onMain(block: () -> T): T {
        if (Looper.myLooper() == Looper.getMainLooper()) return block()
        val latch = CountDownLatch(1)
        var res: Any? = null
        var err: Throwable? = null
        main.post {
            try { res = block() } catch (t: Throwable) { err = t } finally { latch.countDown() }
        }
        latch.await()
        err?.let { throw it }
        return res as T
    }

    /** Izvrši JS i vrati rezultat kao običan string (WebView ga vraća JSON-kodiranog). */
    private fun evalJs(wv: WebView, js: String, waitMs: Long = 4000): String {
        val latch = CountDownLatch(1)
        var out = ""
        main.post {
            try {
                wv.evaluateJavascript(js) { v ->
                    out = try {
                        val x = JSONTokener(v ?: "null").nextValue()
                        if (x == null || x == JSONObject.NULL) "" else x.toString()
                    } catch (e: Exception) { "" }
                    latch.countDown()
                }
            } catch (e: Exception) { latch.countDown() }
        }
        latch.await(waitMs, TimeUnit.MILLISECONDS)
        return out
    }

    fun resolve(
        url: String, rules: RuleSet?, adblock: Boolean, adHosts: List<String>, selectors: List<String>,
        referer: String?, episode: Int, clickDelayMs: Long = 6_000, timeoutMs: Long = 40_000, log: (String) -> Unit = {},
    ): Resolved {
        val found = CopyOnWriteArrayList<String>()
        val others = CopyOnWriteArrayList<String>()
        val ignore = java.util.Collections.synchronizedSet(HashSet<String>())
        val firstOther = java.util.concurrent.atomic.AtomicLong(0L)
        val useRules = rules != null && rules.active

        fun isAdUrl(u: String) = adblock && AdBlock.isAd(u, adHosts) && !(useRules && rules!!.match(u, false))

        fun check(u: String) {
            if (u.startsWith("data:") || u.startsWith("blob:") || ignore.contains(u)) return
            if (isAdUrl(u)) return
            val gen = genericMatch(u)
            if (useRules) {
                if (rules!!.match(u, gen)) {
                    if (!found.contains(u)) { found.add(u); log("nađen link po pravilu: " + u.take(110)) }
                } else if (gen && !others.contains(u)) {
                    others.add(u)
                    firstOther.compareAndSet(0L, System.currentTimeMillis())
                }
            } else if (gen && !found.contains(u)) {
                found.add(u)
                log("nađen kandidat: " + u.take(110))
            }
        }

        val pageLoaded = java.util.concurrent.atomic.AtomicBoolean(false)
        var wv: WebView? = null
        try {
            wv = onMain {
                val w = WebView(ctx)
                val st = w.settings
                st.javaScriptEnabled = true
                st.domStorageEnabled = true
                st.mediaPlaybackRequiresUserGesture = false
                st.userAgentString = UA
                st.mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
                st.javaScriptCanOpenWindowsAutomatically = false
                st.setSupportMultipleWindows(false)
                CookieManager.getInstance().setAcceptThirdPartyCookies(w, true)
                w.webChromeClient = WebChromeClient()
                w.webViewClient = object : WebViewClient() {
                    override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                        val u = request.url.toString()
                        if (isAdUrl(u)) {
                            return WebResourceResponse("text/plain", "utf-8", ByteArrayInputStream(ByteArray(0)))
                        }
                        check(u)
                        return null
                    }

                    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean =
                        isAdUrl(request.url.toString())

                    override fun onPageFinished(view: WebView, u: String?) { pageLoaded.set(true) }

                    override fun onPageStarted(view: WebView, u: String?, favicon: Bitmap?) {
                        view.evaluateJavascript("window.open=function(){return null};", null)
                    }
                }
                if (!referer.isNullOrEmpty()) w.loadUrl(url, mapOf("Referer" to referer)) else w.loadUrl(url)
                w
            }
            val web: WebView = wv!!
            log("Otvaram $url")

            // pričekaj da se stranica i njene skripte učitaju prije klika (prebrz klik na play često ne uspije)
            val loadDeadline = System.currentTimeMillis() + 10_000
            while (!pageLoaded.get() && System.currentTimeMillis() < loadDeadline) Thread.sleep(200)
            if (clickDelayMs > 0) { log("čekam prije klika na play…"); Thread.sleep(clickDelayMs) }

            val deadline = System.currentTimeMillis() + timeoutMs
            val sels = JSONArray(selectors + PLAY_SELECTORS).toString()
            var clickedAt = 0L
            var okClick = false
            var readyAt = System.currentTimeMillis()
            var needSwitch = episode > 1
            var switched = false

            while (System.currentTimeMillis() < deadline && (found.isEmpty() || needSwitch)) {
                if (!needSwitch && firstOther.get() != 0L && System.currentTimeMillis() - firstOther.get() > OTHERS_GRACE_MS) break
                if (needSwitch && System.currentTimeMillis() >= readyAt) {
                    val before = found + others
                    val st = evalJs(web, String.format(SWITCH_JS, episode))
                    if (st == "clicked") {
                        log("izabrana epizoda $episode")
                        ignore.addAll(before)
                        found.removeAll(ignore.toSet())
                        others.removeAll(ignore.toSet())
                        firstOther.set(0L)
                        needSwitch = false
                        switched = true
                    } else if (st == "active") {
                        needSwitch = false
                    }
                }
                if (!switched && System.currentTimeMillis() - clickedAt > (if (okClick) 6000 else 3000)) {
                    if (evalJs(web, String.format(CLICK_JS, sels)) == "clicked") {
                        log("kliknut play")
                        okClick = true
                        readyAt = System.currentTimeMillis() + clickDelayMs
                    }
                    clickedAt = System.currentTimeMillis()
                }
                Thread.sleep(500)
                // adresa okvira (iframe) sadrži i dio iza #, koji se ne vidi u mrežnim zahtjevima
                val scan = evalJs(web, SCAN_JS)
                if (scan.isNotEmpty()) {
                    try {
                        val o = JSONObject(scan)
                        val fr = o.optJSONArray("iframes")
                        if (fr != null) for (i in 0 until fr.length()) {
                            val s = fr.optString(i)
                            if (s.startsWith("http")) check(s)
                        }
                        val v = o.optString("video")
                        if (v.startsWith("http")) check(v)
                    } catch (e: Exception) { }
                }
            }

            if (found.isNotEmpty()) Thread.sleep(1200)  // pričekaj eventualni master playlist
            if (needSwitch) throw NotFound("Lista epizoda nije pronađena (epizoda $episode).", others.toList())

            val media = pick(found.toList())
            if (media.isEmpty()) {
                if (useRules && others.isNotEmpty()) {
                    throw NotFound("Nijedan link ne odgovara pravilima (pronađeni su drugi video linkovi).", others.toList())
                }
                throw NotFound("Link videa nije pronađen.", others.toList())
            }
            val title = evalJs(web, TITLE_JS).trim()
            val pageUrl = onMain { web.url ?: url }
            val eps = evalJs(web, COUNT_JS).toIntOrNull() ?: 0
            return Resolved(title, media, pageUrl, UA, others.toList(), eps)
        } finally {
            val w = wv
            if (w != null) {
                try { onMain { w.stopLoading(); w.destroy() } } catch (e: Exception) { }
            }
        }
    }
}
