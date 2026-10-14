// Na Androidu zamjenjuje pywebview: pozive api.metoda(...) šalje Kotlin kodu (AndroidBridge) i vraća Promise.
// Na desktopu (nema AndroidBridge) ne radi ništa.
(function () {
  if (!window.AndroidBridge || window.pywebview) return;
  const pending = {};
  let seq = 0;
  window.__cb = function (id, ok, data) {
    const p = pending[id]; delete pending[id];
    if (!p) return;
    if (ok) p.res(data); else p.rej(new Error(String(data)));
  };
  const api = new Proxy({}, {
    get: (_, name) => {
      if (typeof name !== "string" || name === "then") return undefined;
      return (...args) => new Promise((res, rej) => {
        const id = ++seq; pending[id] = { res, rej };
        window.AndroidBridge.call(id, name, JSON.stringify(args));
      });
    },
  });
  window.pywebview = { api };

  const root = document.documentElement;
  root.classList.add("android");
  let tv = false; try { tv = window.AndroidBridge.isTv(); } catch (e) {}
  if (tv) root.classList.add("tv");

  const css = document.createElement("style");
  css.textContent = `
    html.android { -webkit-tap-highlight-color:transparent; }
    html.android button, html.android select { min-height:40px; }
    html.android :focus-visible, html.android button:focus, html.android select:focus { outline:3px solid #f5c518; outline-offset:2px; }
    html.tv { font-size:19px; }
    html.tv button, html.tv select { min-height:46px; }`;
  document.head.appendChild(css);

  document.addEventListener("DOMContentLoaded", () => {
    window.dispatchEvent(new Event("pywebviewready"));
  });
})();
