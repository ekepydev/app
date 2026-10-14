package com.urlvp

import android.net.Uri
import android.webkit.WebResourceResponse
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.ByteArrayInputStream
import java.net.URL
import java.security.SecureRandom
import java.util.Collections
import java.util.concurrent.TimeUnit

class Sess(val referer: String, val ua: String, val origin: String)

/** HTTP klijent + lokalni "proxy": video ide kroz /p?s=sesija&u=link da bi se poslali isti Referer i User-Agent kao stranica. */
object Net {
    val client: OkHttpClient = OkHttpClient.Builder()
        .followRedirects(true).followSslRedirects(true)
        .connectTimeout(15, TimeUnit.SECONDS).readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val sessions: MutableMap<String, Sess> = Collections.synchronizedMap(
        object : LinkedHashMap<String, Sess>() {
            override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Sess>?): Boolean = size > 300
        }
    )
    private val rnd = SecureRandom()

    fun newSession(referer: String?, ua: String?): String {
        val b = ByteArray(9)
        rnd.nextBytes(b)
        val sid = android.util.Base64.encodeToString(b, android.util.Base64.URL_SAFE or android.util.Base64.NO_WRAP or android.util.Base64.NO_PADDING)
        var origin = ""
        try {
            val u = Uri.parse(referer ?: "")
            if (!u.scheme.isNullOrEmpty() && !u.encodedAuthority.isNullOrEmpty()) origin = u.scheme + "://" + u.encodedAuthority
        } catch (e: Exception) { }
        sessions[sid] = Sess(referer ?: "", ua ?: "", origin)
        return sid
    }

    fun hasSession(sid: String?): Boolean = sid != null && sessions.containsKey(sid)

    fun proxyPath(url: String, sid: String): String = "/p?s=" + sid + "&u=" + Uri.encode(url)

    private fun join(base: String, ref: String): String = try {
        URL(URL(base), ref).toString()
    } catch (e: Exception) {
        ref
    }

    /** Svaki URI u playlisti (segmenti, podliste, ključevi) usmjeri kroz proxy. */
    fun rewriteM3u8(text: String, base: String, sid: String): String {
        val uriAttr = Regex("URI=\"([^\"]+)\"")
        val sb = StringBuilder()
        for (line in text.lines()) {
            val s = line.trim()
            when {
                s.isEmpty() -> sb.append(line)
                s.startsWith("#") -> sb.append(uriAttr.replace(line) { m ->
                    "URI=\"" + proxyPath(join(base, m.groupValues[1]), sid) + "\""
                })
                else -> sb.append(proxyPath(join(base, s), sid))
            }
            sb.append('\n')
        }
        return sb.toString()
    }

    private fun err(code: Int, msg: String) = WebResourceResponse(
        "text/plain", "utf-8", code, if (code == 403) "Forbidden" else "Bad Gateway",
        mapOf("Cache-Control" to "no-store"), ByteArrayInputStream(msg.toByteArray())
    )

    fun handle(uri: Uri, reqHeaders: Map<String, String>): WebResourceResponse {
        val sid = uri.getQueryParameter("s") ?: ""
        val target = uri.getQueryParameter("u") ?: ""
        val sess = sessions[sid]
        if (sess == null || !(target.startsWith("http://", true) || target.startsWith("https://", true))) {
            return err(403, "forbidden")
        }
        if (AdBlock.on && AdBlock.isAd(target)) return err(403, "blocked (adblock)")
        try {
            val rb = Request.Builder().url(target)
                .header("User-Agent", if (sess.ua.isEmpty()) "Mozilla/5.0" else sess.ua)
                .header("Accept", "*/*")
                .header("Accept-Encoding", "identity")
            if (sess.referer.isNotEmpty()) rb.header("Referer", sess.referer)
            if (sess.origin.isNotEmpty()) rb.header("Origin", sess.origin)
            for ((k, v) in reqHeaders) if (k.equals("Range", true)) rb.header("Range", v)
            val resp = client.newCall(rb.build()).execute()
            val body = resp.body ?: return err(502, "empty body")
            val code = if (resp.code in 300..399 || resp.code < 100) 502 else resp.code
            val ctype = resp.header("Content-Type") ?: ""
            val finalUrl = resp.request.url.toString()
            val isPlaylist = Uri.parse(finalUrl).path.orEmpty().lowercase().endsWith(".m3u8") || ctype.lowercase().contains("mpegurl")
            if (isPlaylist && code < 400) {
                val data = rewriteM3u8(body.string(), finalUrl, sid).toByteArray(Charsets.UTF_8)
                return WebResourceResponse(
                    "application/vnd.apple.mpegurl", "utf-8", 200, "OK",
                    mapOf("Access-Control-Allow-Origin" to "*", "Cache-Control" to "no-store"),
                    ByteArrayInputStream(data)
                )
            }
            val headers = HashMap<String, String>()
            for (h in listOf("Content-Length", "Content-Range", "Accept-Ranges")) resp.header(h)?.let { headers[h] = it }
            headers["Access-Control-Allow-Origin"] = "*"
            headers["Cache-Control"] = "no-store"
            val reason = if (resp.message.isNotEmpty()) resp.message else if (code == 206) "Partial Content" else "OK"
            val mime = if (ctype.isEmpty()) "application/octet-stream" else ctype.substringBefore(";").trim()
            return WebResourceResponse(mime, null, code, reason, headers, body.byteStream())
        } catch (e: Exception) {
            return err(502, "upstream error: " + e.message)
        }
    }
}
