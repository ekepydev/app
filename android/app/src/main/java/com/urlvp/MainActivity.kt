package com.urlvp

import android.app.Activity
import android.app.AlertDialog
import android.app.UiModeManager
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.content.res.Configuration
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.FrameLayout
import androidx.webkit.WebViewAssetLoader
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

/** Jedan ekran: WebView koji prikazuje isto sučelje kao desktop verzija (web/index.html iz assets-a). */
class MainActivity : Activity() {
    private lateinit var web: WebView
    private lateinit var root: FrameLayout
    private lateinit var api: Api
    private val pool = Executors.newCachedThreadPool()

    private var customView: View? = null
    private var customCb: WebChromeClient.CustomViewCallback? = null
    private var pickCb: ((Uri?) -> Unit)? = null
    private var fileCb: ValueCallback<Array<Uri>>? = null

    private val host = "appassets.androidplatform.net"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        root = FrameLayout(this)
        web = WebView(this)
        root.addView(web, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(root)

        api = Api(this)
        val loader = WebViewAssetLoader.Builder()
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(this))
            .build()

        val st = web.settings
        st.javaScriptEnabled = true
        st.domStorageEnabled = true
        st.mediaPlaybackRequiresUserGesture = false
        st.mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
        st.javaScriptCanOpenWindowsAutomatically = true  // da se pop-up pokuša i može prebrojati
        st.setSupportMultipleWindows(true)
        st.allowFileAccess = false

        web.addJavascriptInterface(Bridge(), "AndroidBridge")
        web.webViewClient = object : WebViewClient() {
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                val u = request.url
                if (u.host == host && u.path == "/p") return Net.handle(u, request.requestHeaders)
                return loader.shouldInterceptRequest(u)
            }

            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                // stranica u okviru ne smije otvoriti druge aplikacije/browser (reklame)
                val u = request.url
                if (u.host == host) return false
                if (request.isForMainFrame) { api.blocked++; return true }
                return false
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onCreateWindow(view: WebView, isDialog: Boolean, isUserGesture: Boolean, resultMsg: android.os.Message): Boolean {
                api.blocked++  // novi prozor (pop-up) se ne otvara, samo se broji
                return false
            }

            override fun onShowCustomView(view: View, callback: CustomViewCallback) {
                if (customView != null) { callback.onCustomViewHidden(); return }
                customView = view; customCb = callback
                root.addView(view, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
                web.visibility = View.GONE
                immersive(true)
            }

            override fun onHideCustomView() { hideCustom() }

            override fun onShowFileChooser(w: WebView, cb: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                fileCb?.onReceiveValue(null)
                fileCb = cb
                return try {
                    startActivityForResult(params.createIntent(), 42)
                    true
                } catch (e: Exception) {
                    fileCb = null
                    false
                }
            }
        }
        web.loadUrl("https://$host/assets/index.html")
    }

    // ---------------- most JS <-> Kotlin ----------------
    inner class Bridge {
        @JavascriptInterface
        fun isTv(): Boolean =
            (getSystemService(Context.UI_MODE_SERVICE) as UiModeManager).currentModeType == Configuration.UI_MODE_TYPE_TELEVISION

        @JavascriptInterface
        fun setFullscreen(on: Boolean) {
            runOnUiThread { immersive(on) }
        }

        @JavascriptInterface
        fun call(id: Int, method: String, argsJson: String) {
            pool.execute {
                val args = try { JSONArray(argsJson) } catch (e: Exception) { JSONArray() }
                try {
                    api.call(method, args) { res -> reply(id, true, res) }
                } catch (t: Throwable) {
                    reply(id, false, t.toString())
                }
            }
        }
    }

    private fun reply(id: Int, ok: Boolean, res: Any?) {
        val json = when (res) {
            null -> "null"
            is JSONObject -> res.toString()
            is JSONArray -> res.toString()
            is Boolean, is Int, is Long, is Double -> res.toString()
            else -> JSONObject.quote(res.toString())
        }
        runOnUiThread { web.evaluateJavascript("window.__cb($id,$ok,$json)", null) }
    }

    // ---------------- pomoć za Api ----------------
    /** Otvori birač fajlova (SAF). false = ovaj uređaj nema birač (npr. neki TV). */
    fun pickList(cb: (Uri?) -> Unit): Boolean {
        pickCb = cb
        return try {
            val i = Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*")
            i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
            startActivityForResult(i, 41)
            true
        } catch (e: ActivityNotFoundException) {
            pickCb = null
            false
        }
    }

    fun askText(title: String, cb: (String?) -> Unit) {
        runOnUiThread {
            val input = EditText(this)
            input.setSingleLine(true)
            AlertDialog.Builder(this).setTitle(title).setView(input)
                .setPositiveButton("OK") { _, _ -> cb(input.text.toString()) }
                .setNegativeButton("Odustani") { _, _ -> cb(null) }
                .setOnCancelListener { cb(null) }
                .show()
        }
    }

    fun openExternal(url: String) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: Exception) { }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        when (requestCode) {
            41 -> {
                val cb = pickCb
                pickCb = null
                val uri = if (resultCode == RESULT_OK) data?.data else null
                if (uri != null) {
                    try { contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) } catch (e: Exception) { }
                }
                cb?.invoke(uri)
            }
            42 -> {
                val cb = fileCb
                fileCb = null
                cb?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data))
            }
        }
    }

    // ---------------- cijeli ekran i tipka nazad ----------------
    private fun immersive(on: Boolean) {
        window.decorView.systemUiVisibility = if (on) {
            View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
        } else View.SYSTEM_UI_FLAG_VISIBLE
    }

    private fun hideCustom() {
        val v = customView ?: return
        root.removeView(v)
        customView = null
        customCb?.onCustomViewHidden()
        customCb = null
        web.visibility = View.VISIBLE
        immersive(false)
    }

    override fun onBackPressed() {
        if (customView != null) { hideCustom(); return }
        // sučelje prvo zatvori dijalog/player/detalje; tek ako nema ničega otvorenog, aplikacija se zatvara
        web.evaluateJavascript("(function(){return !!(window.__onBack && window.__onBack());})()") { v ->
            if (v != "true") finish()
        }
    }

    override fun onResume() { super.onResume(); web.onResume() }
    override fun onPause() { web.onPause(); super.onPause() }
    override fun onDestroy() { web.destroy(); super.onDestroy() }
}
