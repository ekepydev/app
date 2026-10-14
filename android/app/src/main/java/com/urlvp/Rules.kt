package com.urlvp

import android.net.Uri
import org.json.JSONArray
import org.json.JSONObject

/** Pravila po kojima se bira pravi link videa (isto kao rules.py). */
object Rules {
    private val TOKEN = Regex("^[A-Za-z0-9_\\-=.%~]{12,}$")
    private val MEDIA_EXT = Regex("\\.(m3u8|mp4|webm|mpd|mkv|mov|ts|m4s)$", RegexOption.IGNORE_CASE)
    private val PARTS = Regex("^([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)([^?#]*)(\\?[^#]*)?(#.*)?$")

    private fun looksLikeToken(seg: String) = TOKEN.matches(seg) && !MEDIA_EXT.containsMatchIn(seg)

    /** Iz primjera linka napravi uzorak: token na kraju putanje, query i fragment -> * */
    fun makePattern(example: String): String {
        val ex = example.trim()
        val m = PARTS.matchEntire(ex) ?: return ex
        val scheme = m.groupValues[1]
        val host = m.groupValues[2]
        val segs = m.groupValues[3].split("/").toMutableList()
        val query = m.groupValues[4]
        val frag = m.groupValues[5]
        if (segs.isNotEmpty() && segs.last().isNotEmpty() && looksLikeToken(segs.last())) {
            segs[segs.size - 1] = "*"
        }
        return scheme + "://" + host + segs.joinToString("/") +
            (if (query.length > 1) "?*" else "") + (if (frag.length > 1) "#*" else "")
    }

    fun fromInput(text: String): JSONObject? {
        val t = text.trim()
        if (t.isEmpty()) return null
        val o = JSONObject()
        if (t.lowercase().startsWith("http://") || t.lowercase().startsWith("https://")) {
            o.put("kind", "url").put("value", t).put("pattern", if (t.contains("*")) t else makePattern(t))
        } else {
            o.put("kind", "domain").put("value", t).put("pattern", t)
        }
        return o
    }

    fun hostOf(url: String): String = try {
        (Uri.parse(url).host ?: "").lowercase()
    } catch (e: Exception) {
        ""
    }
}

class RuleSet(rules: JSONArray?) {
    private class R(val kind: String, val value: String, val rx: Regex?)

    private val list = ArrayList<R>()

    init {
        if (rules != null) {
            for (i in 0 until rules.length()) {
                val o = rules.optJSONObject(i) ?: continue
                val kind = o.optString("kind")
                list.add(R(kind, o.optString("value"), if (kind == "url") compile(o.optString("pattern")) else null))
            }
        }
    }

    val active: Boolean get() = list.isNotEmpty()

    private fun compile(pattern: String): Regex {
        var pat = pattern.trim().replaceFirst(Regex("^https?://", RegexOption.IGNORE_CASE), "")
        val optionalQuery = pat.endsWith("?*")
        if (optionalQuery) pat = pat.dropLast(2)
        var rx = "^https?://" + pat.split("*").joinToString(".*") { Regex.escape(it) }
        if (optionalQuery) rx += "(\\?.*)?\$"
        return Regex(rx, RegexOption.IGNORE_CASE)
    }

    fun match(url: String, isMedia: Boolean): Boolean {
        for (r in list) {
            if (r.kind == "domain") {
                if (isMedia && Rules.hostOf(url).contains(r.value.trim().lowercase())) return true
            } else if (r.rx != null && r.rx.containsMatchIn(url)) {
                return true
            }
        }
        return false
    }
}
