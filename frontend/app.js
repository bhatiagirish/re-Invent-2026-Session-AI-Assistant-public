/* app.js — bootstrap: wire modules, check auth, preload favorite state. */
(function () {
  function wireDetailsModal() {
    const backdrop = document.getElementById("details-backdrop");
    document.getElementById("details-close")
      .addEventListener("click", () => window.Sessions.closeDetails());
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) window.Sessions.closeDetails();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !backdrop.hidden) window.Sessions.closeDetails();
    });
  }

  // Register the personal-time create/update/delete handlers (Req 23.1). The
  // input form (task 10.3) calls these through window.ScheduleActions; each
  // delegates to ScheduleView, which refreshes the view only on confirmed
  // success and leaves it unchanged on failure/timeout.
  function registerScheduleHandlers() {
    if (!window.ScheduleView) return;
    window.ScheduleActions = {
      create: (entry) => window.ScheduleView.createPersonalTime(entry),
      update: (id, entry) => window.ScheduleView.updatePersonalTime(id, entry),
      remove: (id) => window.ScheduleView.deletePersonalTime(id),
    };
  }

  async function init() {
    if (window.Theme) window.Theme.wire();  // apply saved/default theme + toggle
    window.Auth.wire();
    window.Chat.wire();
    wireDetailsModal();
    if (window.ScheduleView) window.ScheduleView.wire();
    registerScheduleHandlers();

    // Native-connect handoff: if connect.py sent us here with #connect=<code>,
    // exchange it for the session cookie (first-party on this origin), then
    // clear the fragment so the code isn't left in the address bar.
    const m = (window.location.hash || "").match(/(?:^#|&)connect=([^&]+)/);
    if (m) {
      try { await window.API.claim(decodeURIComponent(m[1])); }
      catch (_) { /* invalid/expired code -> user just sees Not Connected */ }
      history.replaceState({}, "", window.location.pathname);
    }

    const state = await window.Auth.checkAuthStatus();
    // checkAuthStatus() runs AFTER Chat.wire()'s first renderSuggestions(), so
    // flag-gated chips (e.g. "My Reservations", which depends on
    // reservationsEnabled()) are not yet known at first render. Re-render the
    // suggestions now that the backend flag has resolved.
    if (window.Chat && window.Chat.renderSuggestions) window.Chat.renderSuggestions();
    const authorized = state.authenticated && state.eventAccess === "authorized";
    // Show the persistent "My Schedule" control only in the authorized state
    // (Req 23.1), so the schedule is never surfaced for an unauthorized session.
    if (window.ScheduleView) window.ScheduleView.setAuthorized(authorized);
    if (authorized) {
      // Preload just the favorite id set (fast: one GetSchedule call) so cards
      // render with the correct star state. No per-session detail fetches.
      window.Favorites.loadFavoriteIds();
      // Preload reserved id set in parallel so reserve/unreserve buttons render
      // in the correct state. Guarded on the flag (Req 8.2): when reservations
      // are disabled, enabled() is false and no reserved-ids request is made.
      if (window.Reservations && window.Reservations.enabled && window.Reservations.enabled()) {
        window.Reservations.loadReservedIds();
      }
    }

    // Surface a post-login auth error passed back via ?auth_error=1.
    if (new URLSearchParams(window.location.search).get("auth_error")) {
      window.Chat.notify("Sign-in could not be completed. Please try again.", true);
      history.replaceState({}, "", window.location.pathname);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
