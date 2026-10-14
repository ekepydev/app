package com.urlvp

import android.net.Uri
import android.provider.OpenableColumns
import android.util.Base64
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors

class Item(
    val url: String, var name: String, var watched: Boolean, var isNew: Boolean,
    val series: Boolean, var ep: Int, var eps: Int, val done: MutableSet<Int>,
) {
    val cache = HashMap<Int, Cached>()
    var mk: String = ""        // ključ za TMDB (naslov bez sezone/ID-a)
    var base: String = ""
    var season: Int? = null
    var year: Int? = null
}

class Cached(val media: String, val ts: Long, val referer: String, val ua: String, var sid: String?, val note: String)

/** Isti API kao u app.py (pywebview), samo što ga sučelje poziva preko AndroidBridge-a. */
class Api(private val act: MainActivity) {
    companion object {
        val SERIES_RE = Regex("/season/", RegexOption.IGNORE_CASE)
        val MEDIA_LINK = Regex("\\.(m3u8|mp4|webm|mkv|mov|m4v|ogv|ogg|mpd)(\\?|\$)", RegexOption.IGNORE_CASE)
        const val MEDIA_TTL = 15 * 60 * 1000L
    }

    private val lock = Any()
    private val io = Executors.newSingleThreadExecutor()
    private val settingsFile = File(act.filesDir, "settings.json")
    private val subsDir = File(act.filesDir, "subs")
    private val resolver = Resolver(act)
    private var s: JSONObject = loadSettings()
    private val meta = Meta(File(act.filesDir, "meta_cache.json"), { s.optString("tmdb_key") }, { s.optString("meta_lang", "hr-HR") })
    private val items = ArrayList<Item>()
    private val byUrl = HashMap<String, Item>()
    private var version = 0
    private var message = ""
    private val cands = ArrayList<String>()
    @Volatile var blocked = 0

    private var loaded = false

    init {
        syncAd()
    }

    /** Lista se čita iz radne niti pri prvom pozivu sučelja (čitanje fajla/mreže ne smije na glavnu nit). */
    private fun ensureLoaded() {
        val go = synchronized(lock) { if (loaded) false else { loaded = true; true } }
        if (go && s.getJSONArray("lists").length() > 0) loadList()
    }

    // ---------------- postavke ----------------
    private fun loadSettings(): JSONObject {
        val o = try { JSONObject(settingsFile.readText()) } catch (e: Exception) { JSONObject() }
        fun d(k: String, v: Any) { if (!o.has(k)) o.put(k, v) }
        d("list_path", ""); d("list_name", ""); d("lists", JSONArray())
        if (o.getJSONArray("lists").length() == 0 && o.optString("list_path").isNotEmpty()) {
            o.getJSONArray("lists").put(JSONObject().put("path", o.optString("list_path")).put("name", o.optString("list_name")))
        }
         d("rules", JSONArray()); d("click_selectors", JSONArray())
        d("items", JSONObject()); d("last", ""); d("autoplay", true); d("skip_watched", false)
        d("show_browser", false); d("adblock", true); d("extract_stream", false); d("ad_hosts", JSONArray())
        d("os_api_key", ""); d("os_user", ""); d("os_pass", ""); d("sub_langs", "hr,bs,sr"); d("auto_subs", "off")
        d("tmdb_key", ""); d("meta_lang", "hr-HR")
        if (o.optString("auto_subs") != "off") o.put("auto_subs", "online")
        return o
    }

    private fun save() {
        io.execute {
            try {
                val text = synchronized(lock) { s.toString() }
                settingsFile.writeText(text)
            } catch (e: Exception) { }
        }
    }

    private fun syncAd() {
        AdBlock.on = s.optBoolean("adblock", true)
        AdBlock.hosts = strings(s.optJSONArray("ad_hosts"))
    }

    private fun strings(a: JSONArray?): List<String> {
        val out = ArrayList<String>()
        if (a != null) for (i in 0 until a.length()) out.add(a.optString(i))
        return out
    }

    private fun bump() { version++ }

    // ---------------- lista ----------------
    private fun readList(path: String): String {
        return when {
            path.startsWith("http://") || path.startsWith("https://") ->
                Net.client.newCall(Request.Builder().url(path).build()).execute().use { it.body?.string() ?: "" }
            path.startsWith("content://") ->
                act.contentResolver.openInputStream(Uri.parse(path))?.use { String(it.readBytes(), Charsets.UTF_8) } ?: ""
            else -> File(path).readText()
        }
    }

    private fun nameFromUrl(url: String): String {
        val u = Uri.parse(url)
        val parts = (u.path ?: "").split("/").filter { it.isNotEmpty() }
        var slug = if (parts.isNotEmpty()) Uri.decode(parts.last()) else (u.host ?: url)
        slug = slug.replace(Regex("\\.(html?|php|aspx?)\$", RegexOption.IGNORE_CASE), "")
        slug = slug.replace(Regex("[-_+]+"), " ").trim()
        if (slug.isEmpty()) return u.host ?: url
        return slug.split(" ").joinToString(" ") { w -> w.lowercase().replaceFirstChar { c -> c.titlecase() } }
    }

    private fun rec(it: Item): JSONObject {
        val r = JSONObject().put("name", it.name).put("watched", it.watched)
        if (it.series) r.put("ep", it.ep).put("eps", it.eps).put("done", JSONArray(it.done.sorted()))
        return r
    }

    private fun loadList() {
        val lists = synchronized(lock) { s.getJSONArray("lists") }
        val sb = StringBuilder()
        val problems = ArrayList<String>()
        for (i in 0 until lists.length()) {
            val l = lists.getJSONObject(i)
            try { sb.append(readList(l.optString("path"))).append('\n') }
            catch (e: Exception) { problems.add(l.optString("name") + ": " + e.message) }
        }
        if (problems.isNotEmpty()) message = "Ne mogu pročitati listu: " + problems.joinToString("; ")
        val text = sb.toString()
        synchronized(lock) {
            val known = s.getJSONObject("items")
            val hadKnown = known.length() > 0
            val seen = HashSet<String>()
            val out = ArrayList<Item>()
            var added = 0
            for (raw in text.lines()) {
                val line = raw.trim()
                if (!(line.startsWith("http://") || line.startsWith("https://")) || !seen.add(line)) continue
                var item = byUrl[line]
                if (item == null) {
                    val sv = known.optJSONObject(line)
                    val savedName = sv?.optString("name") ?: ""
                    val doneSet = HashSet<Int>()
                    val da = sv?.optJSONArray("done")
                    if (da != null) for (i in 0 until da.length()) doneSet.add(da.optInt(i))
                    item = Item(
                        line, if (savedName.isNotEmpty()) savedName else nameFromUrl(line),
                        sv?.optBoolean("watched") ?: false, hadKnown && sv == null,
                        SERIES_RE.containsMatchIn(line), sv?.optInt("ep", 1) ?: 1, sv?.optInt("eps", 0) ?: 0, doneSet,
                    )
                    val series = item.series
                    val (b, y, se) = Meta.parseName(nameFromUrl(line), series)  // po URL-u, ne po naslovu stranice
                    item.base = b; item.year = y; item.season = se; item.mk = Meta.makeKey(series, b, y)
                    if (item.isNew) added++
                }
                out.add(item)
            }
            items.clear(); items.addAll(out)
            byUrl.clear(); for (i in out) byUrl[i.url] = i
            for (i in out) known.put(i.url, rec(i))
            if (added > 0) message = "Novih videa u listi: $added"
            bump()
        }
        save()
        meta.setItems(synchronized(lock) { items.map { MetaTarget(it.mk, it.series, it.base, it.year) } })
    }

    private fun view(): JSONArray {
        val a = JSONArray()
        for (i in items) {
            val o = JSONObject().put("url", i.url).put("name", i.name).put("watched", i.watched).put("new", i.isNew)
                .put("series", i.series).put("ep", i.ep).put("eps", i.eps).put("done", JSONArray(i.done.sorted()))
                .put("mk", i.mk).put("base", i.base).put("season", i.season ?: JSONObject.NULL).put("year", i.year ?: JSONObject.NULL)
            a.put(o)
        }
        return a
    }

    private fun options(): JSONObject = JSONObject()
        .put("autoplay", s.optBoolean("autoplay")).put("skip_watched", s.optBoolean("skip_watched"))
        .put("show_browser", s.optBoolean("show_browser")).put("adblock", s.optBoolean("adblock"))
        .put("extract_stream", s.optBoolean("extract_stream"))

    private fun state(): JSONObject = synchronized(lock) {
        val lists = s.getJSONArray("lists")
        val names = ArrayList<String>()
        for (i in 0 until lists.length()) names.add(lists.getJSONObject(i).optString("name"))
        val st = JSONObject().put("version", version).put("list_name", names.joinToString(", ")).put("lists", lists)
            .put("items", view()).put("options", options()).put("last", s.optString("last")).put("message", message)
        message = ""
        st
    }

    // ---------------- poziv iz sučelja ----------------
    fun call(method: String, a: JSONArray, done: (Any?) -> Unit) {
        when (method) {
            "init" -> { ensureLoaded(); done(state()) }
            "poll" -> done(synchronized(lock) {
                if (a.optInt(0, -1) == version) JSONObject().put("version", version).put("items", JSONObject.NULL)
                else null
            }.let { it ?: state() })
            "choose_file" -> chooseFile(done)
            "refresh" -> { loadList(); done(state()) }
            "remove_list" -> {
                val path = a.optString(0)
                synchronized(lock) {
                    val ls = s.getJSONArray("lists")
                    for (i in ls.length() - 1 downTo 0) if (ls.getJSONObject(i).optString("path") == path) ls.remove(i)
                }
                loadList(); done(state())
            }
            "set_option" -> {
                synchronized(lock) { s.put(a.optString(0), a.opt(1)) }
                syncAd(); save(); done(true)
            }
            "set_last" -> { synchronized(lock) { s.put("last", a.optString(0)) }; save(); done(true) }
            "mark_watched" -> { mark(a.optString(0), a.optInt(1, 0), true); done(true) }
            "mark_unwatched" -> { mark(a.optString(0), a.optInt(1, 0), false); done(true) }
            "resolve" -> done(resolve(a.optString(0), a.optBoolean(1), a.optInt(2, 0)))
            "open_external" -> { act.openExternal(a.optString(0)); done(true) }
            "blocked_count" -> done(blocked)
            "get_rules" -> done(rulesInfo())
            "preview_rule" -> done(preview(a.optString(0)))
            "add_rule" -> done(addRule(a.optString(0)))
            "delete_rule" -> {
                synchronized(lock) {
                    val r = s.getJSONArray("rules"); val i = a.optInt(0)
                    if (i in 0 until r.length()) { r.remove(i); clearCache() }
                    bump()
                }
                save(); done(rulesInfo())
            }
            "set_click_delay" -> {
                val v = a.optString(0).replace(",", ".").toDoubleOrNull()
                if (v == null) done(false) else {
                    synchronized(lock) { s.put("click_wait", v.coerceIn(0.0, 30.0)); clearCache() }
                    save(); done(true)
                }
            }
            "set_selectors" -> { setList("click_selectors", a.optString(0)); done(rulesInfo()) }
            "set_ad_hosts" -> { setList("ad_hosts", a.optString(0)); syncAd(); done(rulesInfo()) }
            "subs_settings" -> done(JSONObject().put("os_api_key", s.optString("os_api_key")).put("os_user", s.optString("os_user"))
                .put("os_pass", s.optString("os_pass")).put("sub_langs", s.optString("sub_langs")).put("auto_subs", s.optString("auto_subs")))
            "set_subs_settings" -> {
                val o = a.optJSONObject(0) ?: JSONObject()
                synchronized(lock) {
                    for (k in listOf("os_api_key", "os_user", "os_pass", "sub_langs", "auto_subs")) if (o.has(k)) s.put(k, o.get(k))
                }
                save(); done(true)
            }
            "meta_init" -> {
                val g = JSONObject()
                for ((k, v) in Meta.GENRES) g.put(k.toString(), v)
                done(JSONObject().put("genres", g).put("status", meta.status()).put("has_key", s.optString("tmdb_key").isNotEmpty())
                    .put("base", Meta.POSTER_BASE))
            }
            "meta_since" -> done(meta.since(a.optInt(0, 0)))
            "meta_status" -> done(meta.status())
            "meta_want" -> {
                val arr = a.optJSONArray(0)
                val l = ArrayList<String>()
                if (arr != null) for (i in 0 until arr.length()) l.add(arr.optString(i))
                meta.want(l); done(true)
            }
            "meta_detail" -> done(try { meta.detail(a.optString(0)) } catch (e: Exception) { null })
            "meta_search" -> done(try {
                JSONObject().put("ok", true).put("results", meta.search(a.optString(0), a.optBoolean(1)))
            } catch (e: MetaAuthError) { err(e.message ?: "Greška") } catch (e: MetaNetError) { err(e.message ?: "Greška") })
            "meta_pick" -> done(try {
                JSONObject().put("ok", true).put("meta", meta.pick(a.optString(0), a.optInt(1), a.optBoolean(2)) ?: JSONObject.NULL)
            } catch (e: MetaAuthError) { err(e.message ?: "Greška") } catch (e: MetaNetError) { err(e.message ?: "Greška") })
            "get_tmdb_key" -> done(JSONObject().put("key", s.optString("tmdb_key")).put("lang", s.optString("meta_lang", "hr-HR")))
            "set_tmdb_key" -> {
                val key = a.optString(0).trim()
                val lang = a.optString(1, "hr-HR").trim().ifEmpty { "hr-HR" }
                synchronized(lock) { s.put("tmdb_key", key).put("meta_lang", lang) }
                save()
                val e = if (key.isNotEmpty()) meta.checkKey() else ""
                meta.error = ""
                meta.want(emptyList())
                done(JSONObject().put("ok", e.isEmpty()).put("error", e))
            }
            "set_fullscreen" -> done(false)  // full screen ide direktno preko AndroidBridge.setFullscreen
            "subs_cached" -> done(Subs.load(subsDir, a.optString(0)))
            "subs_upload" -> done(subsUpload(a.optString(0), a.optString(1)))
            "subs_search" -> done(subsSearch(a.optString(0)))
            "subs_download" -> done(subsDownload(a.optString(0), a.optLong(1)))
            "subs_auto_online" -> done(subsAuto(a.optString(0)))
            else -> throw IllegalArgumentException("Nepoznata metoda: $method")
        }
    }

    private fun err(msg: String): JSONObject = JSONObject().put("ok", false).put("error", msg)

    private fun chooseFile(done: (Any?) -> Unit) {
        val finish = { path: String, name: String ->
            synchronized(lock) {  // lista se DODAJE na postojeće (filmovi + serije), ne zamjenjuje ih
                val ls = s.getJSONArray("lists")
                var has = false
                for (i in 0 until ls.length()) if (ls.getJSONObject(i).optString("path") == path) has = true
                if (!has) ls.put(JSONObject().put("path", path).put("name", name))
            }
            loadList(); done(state())
        }
        val launched = act.pickList { uri ->
            if (uri == null) { done(null) } else {
                var name = "lista.txt"
                try {
                    act.contentResolver.query(uri, null, null, null, null)?.use { c ->
                        val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                        if (i >= 0 && c.moveToFirst()) name = c.getString(i)
                    }
                } catch (e: Exception) { }
                finish(uri.toString(), name)
            }
        }
        if (!launched) {
            // TV bez birača fajlova: upiši link na txt listu (http…), npr. raw link sa GitHuba ili Dropboxa
            act.askText("Link na txt listu (http…)") { text ->
                val t = text?.trim() ?: ""
                if (t.startsWith("http://") || t.startsWith("https://")) finish(t, t.substringAfterLast('/')) else done(null)
            }
        }
    }

    // ---------------- pogledano ----------------
    private fun mark(url: String, ep: Int, watched: Boolean) {
        synchronized(lock) {
            val it = byUrl[url] ?: return
            if (watched) {
                it.isNew = false
                if (it.series && ep > 0) {
                    it.done.add(ep); it.ep = ep
                    if (it.eps > 0 && it.done.size >= it.eps) it.watched = true
                } else it.watched = true
            } else {
                if (it.series && ep > 0) it.done.remove(ep) else it.done.clear()
                it.watched = false
            }
            s.getJSONObject("items").put(url, rec(it))
            bump()
        }
        save()
    }

    // ---------------- traženje linka ----------------
    private fun resolve(url: String, force: Boolean, epArg: Int): JSONObject {
        blocked = 0
        val item = synchronized(lock) { byUrl[url] } ?: return err("Link nije u listi.")
        val ep = if (item.series) (if (epArg > 0) epArg else if (item.ep > 0) item.ep else 1) else 0
        val cached = synchronized(lock) { item.cache[ep] }
        val chosen: Cached
        if (!force && cached != null && System.currentTimeMillis() - cached.ts < MEDIA_TTL) {
            synchronized(lock) { item.ep = ep }
            chosen = cached
        } else {
            synchronized(lock) { item.cache.remove(ep) }
            val rules = RuleSet(s.optJSONArray("rules"))
            val adblock = s.optBoolean("adblock", true)
            val adHosts = strings(s.optJSONArray("ad_hosts"))
            val selectors = strings(s.optJSONArray("click_selectors"))
            val extract = s.optBoolean("extract_stream", false)
            val delayMs = (clickDelay() * 1000).toLong()
            try {
                var res = resolver.resolve(url, rules, adblock, adHosts, selectors, null, ep, delayMs)
                var note = ""
                if (extract && !MEDIA_LINK.containsMatchIn(res.media)) {
                    try {
                        val r2 = resolver.resolve(res.media, null, adblock, adHosts, selectors, res.referer, 0, delayMs)
                        res = Resolved(res.title, r2.media, r2.referer, r2.ua, res.others + r2.others, res.episodes)
                    } catch (e: Exception) { note = e.message ?: "" }
                }
                val nc = Cached(res.media, System.currentTimeMillis(), res.referer, res.ua, null, note)
                synchronized(lock) {
                    item.cache[ep] = nc
                    addCands(res.others)
                    if (item.series) { if (res.episodes > 0) item.eps = res.episodes; item.ep = ep }
                    if (res.title.isNotEmpty()) item.name = res.title
                    s.getJSONObject("items").put(url, rec(item))
                    bump()
                }
                save()
                chosen = nc
            } catch (e: NotFound) {
                synchronized(lock) { addCands(e.candidates) }
                return err(e.message ?: "Link nije nađen.").put("candidates", e.candidates.isNotEmpty())
            } catch (e: Exception) {
                return err("Greška: " + e.message)
            }
        }
        return payload(item, ep, chosen)
    }

    private fun addCands(list: List<String>) {
        for (u in list) { cands.remove(u); cands.add(u) }
        while (cands.size > 40) cands.removeAt(0)
    }

    private fun payload(item: Item, ep: Int, c: Cached): JSONObject {
        var sid = c.sid
        if (sid == null || !Net.hasSession(sid)) { sid = Net.newSession(c.referer, c.ua); c.sid = sid }
        val low = c.media.lowercase()
        val path = (Uri.parse(c.media).path ?: "").lowercase()
        val kind = when {
            path.endsWith(".m3u8") || low.contains(".m3u8") -> "hls"
            path.endsWith(".mpd") -> "dash"
            Regex("\\.(mp4|m4v|webm|mkv|mov|ogv|ogg)(\\?|\$)").containsMatchIn(low) -> "file"
            else -> "embed"
        }
        return JSONObject().put("ok", true).put("src", if (kind == "embed") c.media else Net.proxyPath(c.media, sid))
            .put("kind", kind).put("media", c.media).put("name", item.name).put("ep", ep).put("episodes", item.eps)
            .put("note", if (kind == "embed") c.note else "")
    }

    // ---------------- pravila ----------------
    private fun rulesInfo(): JSONObject = synchronized(lock) {
        JSONObject().put("rules", s.getJSONArray("rules"))
            .put("candidates", JSONArray(cands.reversed()))
            .put("selectors", strings(s.optJSONArray("click_selectors")).joinToString(" ; "))
            .put("ad_hosts", strings(s.optJSONArray("ad_hosts")).joinToString(" ; "))
            .put("click_delay", clickDelay())
    }

    private fun clickDelay(): Double = s.optDouble("click_wait", 6.0).let { if (it.isNaN()) 6.0 else it.coerceIn(0.0, 30.0) }

    private fun preview(text: String): JSONObject {
        val r = Rules.fromInput(text) ?: return JSONObject().put("kind", "").put("pattern", "")
        val kind = r.getString("kind")
        return JSONObject().put("kind", kind).put("pattern", if (kind == "url") r.getString("pattern") else r.getString("value"))
    }

    private fun clearCache() { for (i in items) i.cache.clear() }  // stari linkovi možda ne odgovaraju novom pravilu

    private fun addRule(text: String): JSONObject {
        val r = Rules.fromInput(text)
        if (r != null) {
            synchronized(lock) {
                val rules = s.getJSONArray("rules")
                var dup = false
                for (k in 0 until rules.length()) if (rules.getJSONObject(k).optString("pattern") == r.getString("pattern")) dup = true
                if (!dup) { rules.put(r); clearCache(); bump() }
            }
            save()
        }
        return rulesInfo()
    }

    private fun setList(key: String, text: String) {
        val a = JSONArray()
        for (p in text.split(";")) if (p.trim().isNotEmpty()) a.put(p.trim())
        synchronized(lock) { s.put(key, a) }
        save()
    }

    // ---------------- titlovi ----------------
    private fun splitKey(key: String): Pair<String, Int> {
        val i = key.indexOf("#e")
        if (i < 0) return Pair(key, 0)
        return Pair(key.substring(0, i), key.substring(i + 2).toIntOrNull() ?: 0)
    }

    private fun subsUpload(key: String, b64: String): JSONObject {
        return try {
            val cues = Subs.parse(Subs.decodeBytes(Base64.decode(b64, Base64.DEFAULT)))
            if (cues.length() == 0) return err("Fajl se ne može pročitati kao SRT/VTT.")
            Subs.save(subsDir, key, "file", cues)
            JSONObject().put("ok", true).put("cues", cues)
        } catch (e: Exception) {
            err("Titl se ne može pročitati: " + e.message)
        }
    }

    private fun subsSearch(key: String): JSONObject {
        val (base, ep) = splitKey(key)
        val item = synchronized(lock) { byUrl[base] } ?: return err("Video nije u listi.")
        var name = item.name
        var season: Int? = null
        if (ep > 0) { val p = Subs.splitSeason(name); name = p.first; season = p.second }
        val (query, year) = Subs.cleanTitle(name)
        return try {
            val res = Subs.search(s.optString("os_api_key"), query, s.optString("sub_langs"), year, season, if (ep > 0) ep else null)
            val label = query + (if (year != null) " ($year)" else "") + (if (ep > 0) " S${season ?: "?"}E$ep" else "")
            JSONObject().put("ok", true).put("query", label).put("results", res)
        } catch (e: SubError) {
            err(e.message ?: "Greška")
        }
    }

    private fun subsDownload(key: String, fileId: Long): JSONObject = try {
        val cues = Subs.download(s.optString("os_api_key"), s.optString("os_user"), s.optString("os_pass"), fileId)
        Subs.save(subsDir, key, "online", cues)
        JSONObject().put("ok", true).put("cues", cues)
    } catch (e: SubError) {
        err(e.message ?: "Greška")
    }

    private fun subsAuto(key: String): JSONObject {
        val sr = subsSearch(key)
        if (!sr.optBoolean("ok")) return sr
        val results = sr.getJSONArray("results")
        if (results.length() == 0) return err("Nema rezultata za ovaj naslov.")
        // redoslijed jezika iz postavki ima prednost, pa broj preuzimanja
        val langs = s.optString("sub_langs").split(",").map { it.trim().lowercase() }
        var best = results.getJSONObject(0)
        var bestRank = Int.MAX_VALUE
        for (i in 0 until results.length()) {
            val r = results.getJSONObject(i)
            val rank = langs.indexOf(r.optString("lang").lowercase()).let { if (it < 0) 99 else it }
            if (rank < bestRank) { best = r; bestRank = rank }
        }
        return subsDownload(key, best.optLong("file_id"))
    }
}
