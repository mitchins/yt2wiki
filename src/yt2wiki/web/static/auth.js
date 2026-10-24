// yt2wiki — operator auth helpers. Login/logout forms + CSRF token access.
// Pages include this before their own scripts; everything degrades to the
// login box when the operator is not authenticated.
(() => {
  "use strict";

  const csrfFromMeta = () => document.querySelector('meta[name=csrf-token]')?.content || "";

  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    const csrf = csrfFromMeta();
    if (csrf && (opts.method || "GET").toUpperCase() !== "GET") headers["X-CSRF-Token"] = csrf;
    const r = await fetch(path, { ...opts, headers });
    let data = null;
    try { data = await r.json(); } catch { /* non-JSON error page */ }
    if (!r.ok) throw new Error((data && data.detail) || `Request failed (${r.status})`);
    return data;
  }

  async function login(password) {
    const d = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ password }) });
    return d;
  }

  async function logout() {
    try { await api("/api/auth/logout", { method: "POST", body: "{}" }); } catch { /* already out */ }
    location.reload();
  }

  // Wire every [data-login] form on the page (settings + add share the markup).
  document.querySelectorAll("form[data-login]").forEach((form) => {
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const err = form.querySelector("[data-login-error]");
      const btn = form.querySelector("button[type=submit]");
      const input = form.querySelector("input[name=password]");
      if (err) err.hidden = true;
      btn?.setAttribute("aria-busy", "true");
      try {
        await login(input.value);
        location.reload();
      } catch (e) {
        if (err) { err.textContent = e.message; err.hidden = false; }
      } finally { btn?.removeAttribute("aria-busy"); }
    });
  });

  document.querySelectorAll("[data-logout]").forEach((btn) => {
    btn.addEventListener("click", logout);
  });

  window.yt2wikiAuth = { api, login, logout, csrf: csrfFromMeta };
})();
