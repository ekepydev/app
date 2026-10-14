package com.urlvp

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.File
import java.nio.ByteBuffer
import java.nio.charset.Charset
import java.nio.charset.CharacterCodingException
import java.nio.charset.CodingErrorAction
import java.security.MessageDigest
import java.util.zip.GZIPInputStream

class SubError(msg: String) : Exception(msg)

/** Titlovi: parsiranje SRT/VTT, OpenSubtitles, keš (isto kao subs.py). Cue = {s, e, t}. */
object Subs {
    private const val OS_API = "https://api.opensubtitles.com/api/v1"
    private const val OS_UA = "URLVideoPlayer v1.0"
    private const val BALKAN = "čćšžđČĆŠŽĐ"
    private val TS = Regex("(?:(\\d+):)?(\\d{1,2}):(\\d{2})[.,](\\d{1,3})\\s*-->\\s*(?:(\\d+):)?(\\d{1,2}):(\\d{2})[.,](\\d{1,3})")

    private fun balkan(t: String): Int = t.count { BALKAN.indexOf(it) >= 0 }

    /** Pogodi kodnu stranicu: UTF-8, UTF-16, cp1250, ISO-8859-2, cp852. */
    fun decodeBytes(b: ByteArray): String {
        if (b.size >= 2 && ((b[0] == 0xFF.toByte() && b[1] == 0xFE.toByte()) || (b[0] == 0xFE.toByte() && b[1] == 0xFF.toByte()))) {
            return String(b, Charsets.UTF_16)
        }
        try {
            val dec = Charsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT)
            return dec.decode(ByteBuffer.wrap(b)).toString().removePrefix("\uFEFF")
        } catch (e: CharacterCodingException) { }
        var best: String? = null
        var bestScore = Int.MIN_VALUE
        for (name in listOf("windows-1250", "ISO-8859-2", "IBM852")) {
            try {
                val t = String(b, Charset.forName(name))
                val bad = t.count { it in '\u0080'..'\u009f' || it in '\u2500'..'\u259f' || it == '\uFFFD' }
                val score = balkan(t) - 5 * bad
                if (score > bestScore) { best = t; bestScore = score }
            } catch (e: Exception) { }
        }
        return best ?: String(b, Charsets.ISO_8859_1)
    }

    /** Popravi 'Ä\u008d' i sl. (UTF-8 koji je prije toga pogrešno pročitan kao cp1250/latin-1). */
    fun fixMojibake(t: String): String {
        if (!Regex("[\u00c3\u00c4\u00c5][\u0080-\u00bf\u0100-\u017f\u02c6-\u02dc\u2018-\u203a]").containsMatchIn(t)) return t
        val base = balkan(t)
        for (name in listOf("windows-1250", "windows-1252", "ISO-8859-1")) {
            try {
                val cs = Charset.forName(name)
                if (!cs.newEncoder().canEncode(t)) continue
                val dec = Charsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT)
                val fixed = dec.decode(ByteBuffer.wrap(t.toByteArray(cs))).toString()
                if (balkan(fixed) > base) return fixed
            } catch (e: Exception) { }
        }
        return t
    }

    private fun sec(h: String, m: String, s: String, ms: String): Double =
        (if (h.isEmpty()) 0 else h.toInt()) * 3600.0 + m.toInt() * 60 + s.toInt() + ms.padEnd(3, '0').toInt() / 1000.0

    fun parse(raw: String): JSONArray {
        val text = fixMojibake(raw.replace("\r\n", "\n").replace("\r", "\n"))
        val cues = JSONArray()
        for (block in Regex("\n\\s*\n").split(text)) {
            val lines = block.trim('\n').split("\n")
            for (i in lines.indices) {
                val m = TS.find(lines[i]) ?: continue
                val g = m.groupValues
                val s = sec(g[1], g[2], g[3], g[4])
                val e = sec(g[5], g[6], g[7], g[8])
                val body = lines.drop(i + 1).joinToString("\n")
                    .replace(Regex("<[^>]+>|\\{\\\\[^}]*\\}"), "").trim()
                if (body.isNotEmpty() && e > s) {
                    cues.put(JSONObject().put("s", Math.round(s * 1000) / 1000.0).put("e", Math.round(e * 1000) / 1000.0).put("t", body))
                }
                break
            }
        }
        return cues
    }

    // ---------------- ime filma -> upit ----------------
    private val NOISE = Regex("\\b(full movie|watch online|online free|123movies)\\b", RegexOption.IGNORE_CASE)

    fun splitSeason(name: String): Pair<String, Int?> {
        val m = Regex("[\\s\\-–:]*\\bSeason\\s*(\\d+)\\b", RegexOption.IGNORE_CASE).find(name) ?: return Pair(name, null)
        val base = (name.substring(0, m.range.first) + name.substring(m.range.last + 1)).trim(' ', '-', '–', ':')
        return Pair(base, m.groupValues[1].toInt())
    }

    fun cleanTitle(name: String): Pair<String, Int?> {
        var t = NOISE.replace(name, " ")
        t = Regex("[\\[(].*?[\\])]").replace(t, " ")
        val toks = t.trim().split(Regex("\\s+")).filter { it.isNotEmpty() }.toMutableList()
        var year: Int? = null
        while (toks.size > 1 && toks.last().all { it.isDigit() }) {
            val n = toks.last()
            if (n.length == 4 && n.toInt() in 1900..2099 && year == null) { year = n.toInt(); toks.removeAt(toks.size - 1) }
            else if (n.length >= 4) toks.removeAt(toks.size - 1)
            else break
        }
        return Pair(toks.joinToString(" ").trim(), year)
    }

    // ---------------- OpenSubtitles ----------------
    private fun http(url: String, headers: Map<String, String>, json: JSONObject? = null): JSONObject {
        val rb = Request.Builder().url(url).header("User-Agent", OS_UA).header("Accept", "application/json")
        for ((k, v) in headers) rb.header(k, v)
        if (json != null) rb.post(json.toString().toRequestBody("application/json".toMediaType()))
        try {
            val resp = Net.client.newCall(rb.build()).execute()
            val txt = resp.use { it.body?.string() ?: "" }
            if (!resp.isSuccessful) {
                val detail = try { JSONObject(txt).optString("message") } catch (e: Exception) { "" }
                throw SubError("OpenSubtitles HTTP ${resp.code}. $detail".trim())
            }
            return JSONObject(txt)
        } catch (e: SubError) {
            throw e
        } catch (e: Exception) {
            throw SubError("OpenSubtitles nije dostupan: ${e.message}")
        }
    }

    fun search(apiKey: String, query: String, langs: String, year: Int?, season: Int?, episode: Int?): JSONArray {
        if (apiKey.isEmpty()) throw SubError("Upiši OpenSubtitles API ključ (besplatno na opensubtitles.com) u postavkama titlova.")
        val sb = StringBuilder("$OS_API/subtitles?query=${android.net.Uri.encode(query)}&languages=${android.net.Uri.encode(if (langs.isEmpty()) "en" else langs)}")
        sb.append("&order_by=download_count&order_direction=desc")
        if (year != null) sb.append("&year=$year")
        if (season != null && episode != null) sb.append("&season_number=$season&episode_number=$episode")
        val data = http(sb.toString(), mapOf("Api-Key" to apiKey))
        val out = JSONArray()
        val arr = data.optJSONArray("data") ?: return out
        for (i in 0 until arr.length()) {
            val a = arr.getJSONObject(i).optJSONObject("attributes") ?: continue
            val files = a.optJSONArray("files")
            if (files == null || files.length() == 0) continue
            val f0 = files.getJSONObject(0)
            val rel = a.optString("release").ifEmpty { f0.optString("file_name").ifEmpty { "?" } }
            out.put(JSONObject().put("file_id", f0.optLong("file_id")).put("release", rel)
                .put("lang", a.optString("language")).put("downloads", a.optInt("download_count")))
            if (out.length() >= 25) break
        }
        return out
    }

    fun download(apiKey: String, user: String, pass: String, fileId: Long): JSONArray {
        val headers = HashMap<String, String>()
        headers["Api-Key"] = apiKey
        if (user.isNotEmpty() && pass.isNotEmpty()) {
            val login = http("$OS_API/login", headers, JSONObject().put("username", user).put("password", pass))
            val tok = login.optString("token")
            if (tok.isNotEmpty()) headers["Authorization"] = "Bearer $tok"
        }
        val data = http("$OS_API/download", headers, JSONObject().put("file_id", fileId))
        val link = data.optString("link")
        if (link.isEmpty()) throw SubError(data.optString("message").ifEmpty { "OpenSubtitles nije vratio link (dnevni limit?)." })
        var raw = ByteArray(0)
        try {
            Net.client.newCall(Request.Builder().url(link).header("User-Agent", OS_UA).build()).execute().use { r ->
                raw = r.body?.bytes() ?: ByteArray(0)
            }
        } catch (e: Exception) {
            throw SubError("Preuzimanje titla nije uspjelo: ${e.message}")
        }
        if (raw.size >= 2 && raw[0] == 0x1f.toByte() && raw[1] == 0x8b.toByte()) {
            raw = GZIPInputStream(ByteArrayInputStream(raw)).readBytes()
        }
        val cues = parse(decodeBytes(raw))
        if (cues.length() == 0) throw SubError("Titl je preuzet, ali se ne može pročitati.")
        return cues
    }

    // ---------------- keš ----------------
    private fun file(dir: File, key: String): File {
        val h = MessageDigest.getInstance("SHA-1").digest(key.toByteArray()).joinToString("") { "%02x".format(it) }
        return File(dir, "$h.json")
    }

    fun save(dir: File, key: String, source: String, cues: JSONArray) {
        try {
            dir.mkdirs()
            file(dir, key).writeText(JSONObject().put("v", 2).put("source", source).put("cues", cues).toString())
        } catch (e: Exception) { }
    }

    fun load(dir: File, key: String): JSONObject? = try {
        val d = JSONObject(file(dir, key).readText())
        if (d.optInt("v") == 2 && d.optJSONArray("cues")?.length() ?: 0 > 0) d else null
    } catch (e: Exception) {
        null
    }
}
