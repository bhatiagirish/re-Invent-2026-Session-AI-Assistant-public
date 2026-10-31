/* auth.js — authentication status + login/logout UI wiring. */
(function () {
  const state = {
    authenticated: false,
    eventAccess: "unauthenticated",
    // Phased reservations flag (Req 21.7, 23.8): a non-secret boolean surfaced by
    // GET /auth/status. Defaults false so no reservation controls render until the
    // backend confirms the capability is enabled.
    reservationsEnabled: false,
  };

  function els() {
    return {
      dot: document.getElementById("conn-indicator"),
      text: document.getElementById("conn-text"),
      authBtn: document.getElementById("auth-btn"),
      authPanel: document.getElementById("auth-panel"),
      authTitle: document.getElementById("auth-title"),
      authMessage: document.getElementById("auth-message"),
      chatPanel: document.getElementById("chat-panel"),
      connectBtn: document.getElementById("connect-btn"),
    };
  }

  function renderState() {
    const e = els();
    const authorized = state.authenticated && state.eventAccess === "authorized";
    // Keep the persistent Schedule_View control in sync with authorization on
    // every state transition (sign in / out / retry), not just first load.
    if (window.ScheduleView && typeof window.ScheduleView.setAuthorized === "function") {
      window.ScheduleView.setAuthorized(authorized);
    }
    // Header indicator
    e.dot.className = "status-dot " + (authorized ? "status-ok" : "status-off");
    if (authorized) {
      e.text.textContent = "✓ Connected to re:Invent 2026";
    } else if (state.authenticated) {
      e.text.textContent = "○ Not authorized";
    } else {
      e.text.textContent = "○ Not Connected";
    }

    e.authBtn.hidden = false;
    e.authBtn.textContent = state.authenticated ? "Sign Out" : "Sign In";

    // Panels
    if (authorized) {
      e.authPanel.hidden = true;
      e.chatPanel.hidden = false;
    } else {
      e.chatPanel.hidden = true;
      e.authPanel.hidden = false;
      if (state.authenticated && state.eventAccess === "not_registered") {
        e.authTitle.textContent = "Not authorized for re:Invent 2026";
        e.authMessage.textContent =
          "Your AWS identity is authenticated, but AWS Events does not currently " +
          "authorize access to re:Invent 2026.";
        e.connectBtn.hidden = true;
      } else if (state.authenticated && state.eventAccess === "error") {
        e.authTitle.textContent = "Something went wrong";
        e.authMessage.textContent =
          "We couldn't verify your re:Invent access right now. Please try again.";
        e.connectBtn.hidden = false;
        e.connectBtn.textContent = "Retry";
      } else {
        e.authTitle.textContent = "Connect your AWS identity";
        e.authMessage.innerHTML =
          "This is a single-user personal app. AWS Events uses a local " +
          "(loopback) sign-in, so connect by running:<br>" +
          "<code>python docs/spikes/connect.py</code><br>" +
          "It signs you in with your AWS Builder ID and links this app to your " +
          "identity. This page updates automatically once connected.";
        e.connectBtn.hidden = false;
        e.connectBtn.textContent = "Check connection";
      }
    }
  }

  async function checkAuthStatus() {
    try {
      const { data } = await window.API.authStatus();
      state.authenticated = !!(data && data.authenticated);
      state.eventAccess = (data && data.eventAccess) || "unauthenticated";
      state.reservationsEnabled = !!(data && data.reservationsEnabled);
      if (data && data.csrfToken) window.API.setCsrfToken(data.csrfToken);
    } catch (_) {
      state.authenticated = false;
      state.eventAccess = "unauthenticated";
      // Leave reservationsEnabled unchanged on a status-check failure: the flag
      // reflects a backend capability, not the session, and defaults false.
    }
    renderState();
    return state;
  }

  function login() { checkAuthStatus(); }

  async function logout() {
    try { await window.API.logout(); } catch (_) {}
    state.authenticated = false;
    state.eventAccess = "unauthenticated";
    window.API.setCsrfToken(null);
    renderState();
    if (window.Chat) window.Chat.reset();
  }

  function isAuthorized() {
    return state.authenticated && state.eventAccess === "authorized";
  }

  // Whether the phased reservation capability is enabled (Req 21.7, 23.8). The
  // SPA renders reservation controls only when this is true.
  function reservationsEnabled() {
    return !!state.reservationsEnabled;
  }

  function wire() {
    const e = els();
    e.connectBtn.addEventListener("click", login);
    e.authBtn.addEventListener("click", () => (state.authenticated ? logout() : login()));
  }

  window.Auth = {
    checkAuthStatus, login, logout, isAuthorized, reservationsEnabled,
    renderState, wire, state,
  };
})();
