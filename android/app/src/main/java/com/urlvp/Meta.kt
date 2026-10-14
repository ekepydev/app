package com.urlvp

import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.Normalizer
import java.util.ArrayDeque
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

class MetaAuthError(msg: String) : Exception(msg)
class MetaNetError(msg: String) : Exception(msg)
class MetaTarget(val mk: String, val series: Boolean, val title: String, val year: Int?)

/** Poster, žanrovi, ocjena i opis sa TMDB-a (isto kao meta.py). Zapisi se čuvaju u meta_cache.json. */
class Meta(private val cacheFile: File, private val getKey: () -> String, private val getLang: () -> String) {
    companion object {
        const val API = "https://api.themoviedb.org/3"
        const val POSTER_BASE = "https://image.tmdb.org/t/p/w342"
        private const val MISS_RETRY_S = 14L * 86400
        private const val DELAY_MS = 150L

        val GENRES: Map<Int, String> = linkedMapOf(
            28 to "Akcija", 12 to "Avantura", 16 to "Animirani", 35 to "Komedija", 80 to "Krimi", 99 to "Dokumentarni",
            18 to "Drama", 10751 to "Porodični", 14 to "Fantastika", 36 to "Povijesni", 27 to "Horor", 10402 to "Muzički",
            9648 to "Misterija", 10749 to "Romantika", 878 to "Naučna fantastika", 10770 to "TV film", 53 to "Triler",
            10752 to "Ratni", 37 to "Vestern", 10762 to "Dječiji", 10763 to "Vijesti", 10764 to "Reality",
            10766 to "Sapunica", 10767 to "Talk show",
        )
        private val GENRE_MAP = mapOf(10759 to listOf(28, 12), 10765 to listOf(878, 14), 10768 to listOf(10752))

        fun norm(s: String): String {
            val d = Normalizer.normalize(s, Normalizer.Form.NFD).replace(Regex("\\p{M}+"), "")
            return d.lowercase().replace(Regex("[^a-z0-9]+"), " ").trim()
        }

        /** 'Outer Banks Season 4 39274' -> (Outer Banks, null, 4); 'Film 2019 1234' -> (Film, 2019, null) */
        fun parseName(name: String, series: Boolean): Triple<String, Int?, Int?> {
            val (base, season) = if (series) Subs.splitSeason(name) else Pair(name, null)
            val (title, year) = Subs.cleanTitle(base)
            val t = if (title.isNotEmpty()) title else if (base.isNotEmpty()) base else name
            return Triple(t.trim(), if (series) null else year, season)
        }

        fun makeKey(series: Boolean, title: String, year: Int?): String =
            (if (series) "t:" else "m:") + norm(title) + (if (year != null && !series) ":$year" else "")

        private fun str(d: JSONObject, k: String): String = if (d.isNull(k)) "" else d.optString(k)

        private fun mapGenres(ids: List<Int>): List<Int> {
            val out = ArrayList<Int>()
            for (g in ids) for (x in GENRE_MAP[g] ?: listOf(g)) if (GENRES.containsKey(x) && !out.contains(x)) out.add(x)
            return out
        }
    }

    private val lock = Any()
    private var r = JSONObject()   // mk -> zapis (ili {"miss": sekunde})
    private var o = JSONObject()   // ručne ispravke: mk -> {i, s}
    private var ov = JSONObject()  // opisi po jeziku: mk -> {jezik: tekst}
    private val log = ArrayList<String>()
    private val targets = HashMap<String, MetaTarget>()
    private var order: List<String> = emptyList()
    private var sweep = 0
    private val prio = ArrayDeque<String>()
    private val wake = LinkedBlockingQueue<Int>()
    private var thread: Thread? = null
    @Volatile var error = ""
    @Volatile private var busy: String? = null
    private var dirty = 0
    private var lastSave = System.currentTimeMillis()

    init {
        try {
            val d = JSONObject(cacheFile.readText())
            if (d.optInt("v") == 1) {
                r = d.optJSONObject("r") ?: JSONObject()
                o = d.optJSONObject("o") ?: JSONObject()
                ov = d.optJSONObject("ov") ?: JSONObject()
            }
        } catch (e: Exception) { }
        val it = r.keys()
        while (it.hasNext()) { val k = it.next(); if (!r.getJSONObject(k).has("miss")) log.add(k) }
    }

    val enabled: Boolean get() = getKey().trim().isNotEmpty()

    // ---------------- keš ----------------
    fun save() {
        synchronized(lock) {
            try {
                val tmp = File(cacheFile.path + ".tmp")
                tmp.writeText(JSONObject().put("v", 1).put("r", r).put("o", o).put("ov", ov).toString())
                if (!tmp.renameTo(cacheFile)) { cacheFile.writeText(tmp.readText()); tmp.delete() }
            } catch (e: Exception) { }
            dirty = 0
            lastSave = System.currentTimeMillis()
        }
    }

    private fun store(mk: String, e: JSONObject) {
        synchronized(lock) {
            r.put(mk, e)
            if (!e.has("miss")) log.add(mk)
            dirty++
            if (dirty >= 40 || System.currentTimeMillis() - lastSave > 15000) save()
        }
    }

    private fun miss(): JSONObject = JSONObject().put("miss", System.currentTimeMillis() / 1000)

    // ---------------- zadaci ----------------
    fun setItems(items: List<MetaTarget>) {
        synchronized(lock) {
            targets.clear()
            for (t in items) targets[t.mk] = t
            order = items.map { it.mk }.distinct()
            sweep = 0
        }
        ensureThread()
        wake.offer(1)
    }

    fun want(mks: List<String>) {
        synchronized(lock) {
            for (mk in mks.take(80).reversed()) {
                if (targets.containsKey(mk) && needs(mk) && !prio.contains(mk) && mk != busy) prio.addFirst(mk)
            }
        }
        ensureThread()
        wake.offer(1)
    }

    private fun needs(mk: String): Boolean {
        val e = r.optJSONObject(mk) ?: return true
        return e.has("miss") && System.currentTimeMillis() / 1000 - e.optLong("miss") > MISS_RETRY_S && !o.has(mk)
    }

    private fun next(): String? {
        synchronized(lock) {
            while (prio.isNotEmpty()) { val mk = prio.pollFirst(); if (needs(mk)) return mk }
            while (sweep < order.size) { val mk = order[sweep++]; if (needs(mk)) return mk }
        }
        return null
    }

    fun status(): JSONObject = synchronized(lock) {
        val done = order.count { r.has(it) }
        JSONObject().put("enabled", enabled).put("done", done).put("total", order.size).put("error", error)
    }

    private fun ensureThread() {
        synchronized(lock) {
            if (thread?.isAlive == true) return
            thread = Thread { run() }.also { it.isDaemon = true; it.start() }
        }
    }

    private fun idle(ms: Long) { wake.poll(ms, TimeUnit.MILLISECONDS); wake.clear() }

    private fun run() {
        var backoff = 5000L
        while (true) {
            if (!enabled || error.isNotEmpty()) {
                if (dirty > 0) save()
                idle(3000)
                if (error.isNotEmpty() && enabled) { error = ""; Thread.sleep(30000) }
                continue
            }
            val mk = next()
            if (mk == null) {
                if (dirty > 0) save()
                busy = null
                idle(5000)
                continue
            }
            busy = mk
            try {
                store(mk, fetch(mk))
                backoff = 5000L
            } catch (e: MetaAuthError) {
                error = e.message ?: "TMDB greška"
                synchronized(lock) { prio.addFirst(mk) }
            } catch (e: MetaNetError) {
                synchronized(lock) { prio.addLast(mk) }
                Thread.sleep(backoff)
                backoff = minOf(backoff * 2, 120000L)
            } catch (e: Exception) {
                store(mk, miss())
            }
            Thread.sleep(DELAY_MS)
        }
    }

    // ---------------- TMDB ----------------
    private fun get(path: String, params: Map<String, String> = emptyMap()): JSONObject {
        val key = getKey().trim()
        if (key.isEmpty()) throw MetaAuthError("Upiši TMDB API ključ u postavkama.")
        val ub = (API + path).toHttpUrl().newBuilder()
        for ((k, v) in params) ub.addQueryParameter(k, v)
        val rb = Request.Builder().header("Accept", "application/json").header("User-Agent", "URLVideoPlayer/1.0")
        if (key.length > 40) rb.header("Authorization", "Bearer $key") else ub.addQueryParameter("api_key", key)
        val req = rb.url(ub.build()).build()
        for (attempt in 0 until 3) {
            var code = 0
            var body = ""
            var retryAfter = 2L
            try {
                Net.client.newCall(req).execute().use { resp ->
                    code = resp.code
                    body = resp.body?.string() ?: ""
                    retryAfter = resp.header("Retry-After")?.toLongOrNull() ?: 2L
                }
            } catch (e: Exception) {
                throw MetaNetError("TMDB nije dostupan: ${e.message}")
            }
            when {
                code == 401 -> throw MetaAuthError("TMDB je odbio ključ (401). Provjeri ključ u postavkama.")
                code == 429 -> { Thread.sleep(minOf(30L, retryAfter) * 1000); continue }
                code == 404 -> return JSONObject()
                code !in 200..299 -> throw MetaNetError("TMDB HTTP $code")
            }
            return try { JSONObject(body) } catch (e: Exception) { throw MetaNetError("Neispravan odgovor sa TMDB-a") }
        }
        throw MetaNetError("TMDB je preopterećen (429)")
    }

    private fun entry(d: JSONObject, series: Boolean): JSONObject {
        val date = str(d, if (series) "first_air_date" else "release_date")
        val ids = ArrayList<Int>()
        val gi = d.optJSONArray("genre_ids")
        if (gi != null) {
            for (i in 0 until gi.length()) ids.add(gi.optInt(i))
        } else {
            val gs = d.optJSONArray("genres")
            if (gs != null) for (i in 0 until gs.length()) ids.add(gs.getJSONObject(i).optInt("id"))
        }
        val g = JSONArray()
        for (x in mapGenres(ids)) g.put(x)
        val rating = if (d.isNull("vote_average")) 0.0 else d.optDouble("vote_average", 0.0)
        val year: Any = if (date.length >= 4 && date.substring(0, 4).all { it.isDigit() }) date.substring(0, 4).toInt() else JSONObject.NULL
        return JSONObject().put("i", d.optInt("id")).put("s", if (series) 1 else 0)
            .put("t", str(d, if (series) "name" else "title")).put("y", year).put("p", str(d, "poster_path"))
            .put("g", g).put("r", Math.round(rating * 10) / 10.0).put("o", str(d, "overview"))
    }

    private fun score(res: JSONObject, series: Boolean, title: String, year: Int?): Int {
        val want = norm(title)
        val got = norm(str(res, if (series) "name" else "title"))
        val orig = norm(str(res, if (series) "original_name" else "original_title"))
        var s = 0
        if (want.isNotEmpty() && (want == got || want == orig)) s += 3
        else if (want.isNotEmpty() && got.isNotEmpty() && (got.contains(want) || want.contains(got))) s += 1
        val date = str(res, if (series) "first_air_date" else "release_date").take(4)
        if (year != null && date == year.toString()) s += 2
        return s
    }

    private fun searchBest(series: Boolean, title: String, year: Int?): JSONObject? {
        val kind = if (series) "tv" else "movie"
        val tries: List<Int?> = if (year != null && !series) listOf(year, null) else listOf(null)
        for (y in tries) {
            val p = HashMap<String, String>()
            p["query"] = title; p["include_adult"] = "false"; p["language"] = "en-US"
            if (y != null) p["year"] = y.toString()
            val results = get("/search/$kind", p).optJSONArray("results")
            if (results != null && results.length() > 0) {
                var best: JSONObject? = null
                var bs = -1
                for (i in 0 until minOf(8, results.length())) {
                    val sc = score(results.getJSONObject(i), series, title, year)
                    if (sc > bs) { bs = sc; best = results.getJSONObject(i) }
                }
                return best
            }
        }
        return null
    }

    private fun fetch(mk: String): JSONObject {
        val pair = synchronized(lock) { Pair(targets[mk], o.optJSONObject(mk)) }
        val t = pair.first ?: return miss()
        val ovr = pair.second
        if (ovr != null) {
            val series = ovr.optInt("s") == 1
            val d = get("/${if (series) "tv" else "movie"}/${ovr.getInt("i")}", mapOf("language" to "en-US"))
            return if (d.length() > 0) entry(d, series) else miss()
        }
        val res = searchBest(t.series, t.title, t.year) ?: return miss()
        return entry(res, t.series)
    }

    // ---------------- za sučelje ----------------
    private fun compact(e: JSONObject): JSONObject = JSONObject()
        .put("i", e.opt("i")).put("t", e.opt("t")).put("y", e.opt("y") ?: JSONObject.NULL)
        .put("p", e.opt("p")).put("g", e.opt("g")).put("r", e.opt("r"))

    fun since(ver: Int, limit: Int = 1500): JSONObject = synchronized(lock) {
        val v = maxOf(0, ver)
        val end = minOf(log.size, v + limit)
        val data = JSONObject()
        for (i in v until end) {
            val mk = log[i]
            val e = r.optJSONObject(mk)
            if (e != null && !e.has("miss")) data.put(mk, compact(e))
        }
        JSONObject().put("ver", maxOf(v, end)).put("more", end < log.size).put("base", POSTER_BASE).put("data", data)
    }

    fun detail(mk: String): JSONObject? {
        val e: JSONObject = synchronized(lock) {
            val x = r.optJSONObject(mk)
            if (x == null || x.has("miss")) return null
            JSONObject(x.toString())
        }
        val lang = getLang().ifEmpty { "en-US" }
        var text: String? = null
        if (lang != "en-US" && e.optInt("i") != 0 && enabled) {
            text = synchronized(lock) { ov.optJSONObject(mk)?.let { if (it.has(lang)) it.optString(lang) else null } }
            if (text == null) {
                try {
                    val d = get("/${if (e.optInt("s") == 1) "tv" else "movie"}/${e.getInt("i")}", mapOf("language" to lang))
                    text = str(d, "overview")
                } catch (x: Exception) { text = null }
                if (text != null) synchronized(lock) {
                    val m = ov.optJSONObject(mk) ?: JSONObject().also { ov.put(mk, it) }
                    m.put(lang, text)
                    dirty++
                }
            }
        }
        val out = compact(e)
        out.put("o", if (!text.isNullOrEmpty()) text else str(e, "o"))
        out.put("base", POSTER_BASE)
        return out
    }

    fun search(query: String, series: Boolean): JSONArray {
        val res = get("/search/${if (series) "tv" else "movie"}",
            mapOf("query" to query, "include_adult" to "false", "language" to "en-US")).optJSONArray("results")
        val out = JSONArray()
        if (res != null) for (i in 0 until minOf(12, res.length())) {
            val x = res.getJSONObject(i)
            out.put(compact(entry(x, series)).put("o", str(x, "overview").take(160)))
        }
        return out
    }

    fun pick(mk: String, id: Int, series: Boolean): JSONObject? {
        val d = get("/${if (series) "tv" else "movie"}/$id", mapOf("language" to "en-US"))
        if (d.length() == 0) throw MetaNetError("Naslov nije nađen na TMDB-u.")
        synchronized(lock) {
            o.put(mk, JSONObject().put("i", id).put("s", if (series) 1 else 0))
            ov.remove(mk)
        }
        store(mk, entry(d, series))
        save()
        return detail(mk)
    }

    /** "" ako ključ radi, inače poruka o grešci. */
    fun checkKey(): String = try {
        get("/configuration"); error = ""; ""
    } catch (e: MetaAuthError) { e.message ?: "" } catch (e: MetaNetError) { e.message ?: "" }
}
