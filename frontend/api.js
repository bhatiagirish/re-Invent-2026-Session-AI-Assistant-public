/* api.js — thin fetch wrapper. Sends the HttpOnly session cookie via
 * credentials:'include' and the anti-CSRF token on state-changing requests.
 * The browser talks ONLY to the application backend, never to AWS Events MCP. */
(function () {
  const cfg = window.APP_CONFIG || {};
  const BASE = (cfg.API_BASE_URL || "").replace(/\/$/, "");

  // The CSRF token is a non-secret value returned by /auth/status; kept in memory only.
  let csrfToken = null;
  function setCsrfToken(t) { csrfToken = t || null; }

  function url(path) { return BASE + path; }

  async function request(method, path, body) {
    const headers = { "Accept": "application/json" };
    const opts = { method, credentials: "include", headers };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;

    let res;
    try {
      res = await fetch(url(path), opts);
    } catch (e) {
      const err = new Error("network");
      err.category = "NETWORK";
      throw err;
    }

    let data = null;
    const text = await res.text();
    if (text) { try { data = JSON.parse(text); } catch (_) { data = null; } }

    if (res.status === 401) {
      const err = new Error("unauthenticated");
      err.category = "AUTH_REQUIRED";
      err.status = 401;
      throw err;
    }
    if (!res.ok && res.status >= 500) {
      const err = new Error("server");
      err.category = (data && data.error && data.error.category) || "SERVER";
      err.status = res.status;
      throw err;
    }
    return { status: res.status, data };
  }

  window.API = {
    setCsrfToken,
    authStatus: () => request("GET", "/auth/status"),
    loginUrl: () => url("/auth/login"),
    logout: () => request("POST", "/logout"),
    claim: (claimCode) => request("POST", "/admin/claim", { claimCode }),
    chat: (conversationId, message) => request("POST", "/chat", { conversationId, message }),
    getFavorites: () => request("GET", "/favorites"),
    favoriteIds: () => request("GET", "/favorites/ids"),
    favorite: (sessionId) => request("POST", "/favorites", { sessionId }),
    unfavorite: (sessionId) => request("DELETE", "/favorites/" + encodeURIComponent(sessionId)),
    // Schedule_View + personal-time (GET /schedule is read-only, so no CSRF;
    // the create/update/delete calls are state-changing and carry X-CSRF-Token
    // automatically via request()).
    getSchedule: () => request("GET", "/schedule"),
    createPersonalTime: (entry) => request("POST", "/personal-time", entry),
    updatePersonalTime: (personalTimeId, entry) =>
      request("PUT", "/personal-time/" + encodeURIComponent(personalTimeId), entry),
    deletePersonalTime: (personalTimeId) =>
      request("DELETE", "/personal-time/" + encodeURIComponent(personalTimeId)),
    // Reservations (Phase 11 — PHASED, gated on RESERVATIONS_ENABLED). The
    // backend only registers these routes while the flag is on (404 otherwise);
    // the SPA only calls them when window.Auth.reservationsEnabled() is true.
    // The two GET reads are read-only (no CSRF; cookie via credentials:"include"),
    // mirroring getFavorites/favoriteIds; reserve/cancelReservation are
    // state-changing, so request() carries the X-CSRF-Token header.
    // Catalog index (search over the whole catalog). Builds the index in bounded
    // steps; call again until the response says ready. Makes MCP calls and
    // writes the index, so it is state-changing (carries X-CSRF-Token).
    warmCatalog: () => request("POST", "/catalog/warm"),
    getReservations: () => request("GET", "/reservations"),
    reservedIds: () => request("GET", "/reservations/ids"),
    reserve: (sessionId) => request("POST", "/reservations", { sessionId }),
    cancelReservation: (sessionId) =>
      request("DELETE", "/reservations/" + encodeURIComponent(sessionId)),
  };
})();
