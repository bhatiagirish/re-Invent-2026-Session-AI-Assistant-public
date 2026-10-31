/* chat.js — conversation state, sending messages, rendering, favorites view. */
(function () {
  const SUGGESTIONS = [
    "Show me Amazon Bedrock sessions",
    "Find beginner-friendly serverless sessions",
    "Find Kiro sessions",
  ];

  // re:Invent 2026 runs Mon Nov 30 - Fri Dec 4 (Las Vegas / Pacific). Each chip
  // asks the agent for that single day; the backend show_schedule(date=...)
  // filter returns just that day's items so the reply is short.
  const SCHEDULE_DAYS = [
    { label: "Schedule: Nov 30", date: "2026-11-30" },
    { label: "Schedule: Dec 1",  date: "2026-12-01" },
    { label: "Schedule: Dec 2",  date: "2026-12-02" },
    { label: "Schedule: Dec 3",  date: "2026-12-03" },
    { label: "Schedule: Dec 4",  date: "2026-12-04" },
  ];

  let conversationId = null;
  let sending = false;

  function newConversationId() {
    return "c-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
  }

  function el(id) { return document.getElementById(id); }

  function reset() {
    conversationId = null;
    el("conversation").innerHTML = "";
    renderSuggestions();
  }

  function ensureConversation() {
    if (!conversationId) conversationId = newConversationId();
    return conversationId;
  }

  function renderSuggestions() {
    const box = el("suggestions");
    box.innerHTML = "";
    SUGGESTIONS.forEach((text) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.textContent = text;
      b.addEventListener("click", () => { if (!sending) sendMessage(text); });
      box.appendChild(b);
    });
    // "My Favorites" gets its own affordance.
    const favBtn = document.createElement("button");
    favBtn.type = "button";
    favBtn.className = "chip chip-accent";
    favBtn.textContent = "My Favorites";
    favBtn.addEventListener("click", showFavoritesView);
    box.appendChild(favBtn);

    // "My Reservations" is the parallel affordance to "My Favorites", but it is
    // flag-gated: the chip exists only while the phased reservation capability
    // is enabled (window.Auth.reservationsEnabled()). When the flag is OFF the
    // chip is omitted entirely, so the SPA never offers reserved-session reads.
    if (window.Auth && window.Auth.reservationsEnabled && window.Auth.reservationsEnabled()) {
      const resvBtn = document.createElement("button");
      resvBtn.type = "button";
      resvBtn.className = "chip chip-accent";
      resvBtn.textContent = "My Reservations";
      resvBtn.addEventListener("click", showReservationsView);
      box.appendChild(resvBtn);
    }

    // One-click per-day schedule chips, grouped on their OWN inline row
    // (Nov 30 - Dec 4) so they read as a day-picker separate from the topic
    // suggestions above. The .schedule-day-row wrapper forces the line break.
    const dayRow = document.createElement("div");
    dayRow.className = "schedule-day-row";
    SCHEDULE_DAYS.forEach((d) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip chip-day";
      b.textContent = d.label;
      b.addEventListener("click", () => {
        if (!sending) sendMessage("Show my schedule for " + d.date);
      });
      dayRow.appendChild(b);
    });
    box.appendChild(dayRow);
  }

  // Minimal, XSS-safe Markdown -> HTML for assistant messages. HTML is escaped
  // FIRST, then only a whitelist of inline/block formatting is re-introduced.
  function escapeHtml(t) {
    return String(t == null ? "" : t)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderMarkdown(md) {
    const lines = escapeHtml(md).split(/\r?\n/);
    const html = [];
    let list = null; // "ul" | "ol" | null

    const closeList = () => { if (list) { html.push(`</${list}>`); list = null; } };
    const inline = (t) => t
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");

    for (const raw of lines) {
      const line = raw.trim();
      if (!line) { closeList(); continue; }

      let m;
      if ((m = line.match(/^#{1,6}\s+(.*)$/))) {          // headings -> bold line
        closeList(); html.push(`<div class="md-h">${inline(m[1])}</div>`); continue;
      }
      if ((m = line.match(/^[-*]\s+(.*)$/))) {             // bullet list
        if (list !== "ul") { closeList(); html.push("<ul>"); list = "ul"; }
        html.push(`<li>${inline(m[1])}</li>`); continue;
      }
      if ((m = line.match(/^\d+\.\s+(.*)$/))) {           // numbered list
        if (list !== "ol") { closeList(); html.push("<ol>"); list = "ol"; }
        html.push(`<li>${inline(m[1])}</li>`); continue;
      }
      closeList();
      html.push(`<p>${inline(line)}</p>`);
    }
    closeList();
    return html.join("");
  }

  function appendMessage(role, text) {
    const conv = el("conversation");
    const wrap = document.createElement("div");
    wrap.className = "msg msg-" + role;
    const who = document.createElement("span");
    who.className = "msg-who";
    who.textContent = role === "user" ? "You" : "Assistant";
    const body = document.createElement("div");
    body.className = "msg-body";
    if (role === "assistant") {
      body.innerHTML = renderMarkdown(text);   // safe: escaped, whitelist formatting
    } else {
      body.textContent = text;                  // user input stays literal
    }
    wrap.appendChild(who);
    wrap.appendChild(body);
    conv.appendChild(wrap);
    scroll();
    return wrap;
  }

  function appendSessions(sessions) {
    if (!sessions || !sessions.length) return;
    const conv = el("conversation");
    conv.appendChild(window.Sessions.renderSessionList(sessions));
    // Reflect current favorite state on the freshly-rendered cards.
    window.Sessions.refreshAllCards();
    scroll();
  }

  // Render a Schedule_View ({reserved, favorites, personalTime}) as grouped
  // cards in the conversation, reusing the shared schedule renderer when
  // present (schedule.js) and falling back to session lists otherwise.
  function appendSchedule(schedule) {
    const conv = el("conversation");
    if (window.Schedule && typeof window.Schedule.renderScheduleView === "function") {
      conv.appendChild(window.Schedule.renderScheduleView(schedule, {}));
    } else {
      // Minimal fallback: render reserved + favorites as session cards.
      const all = [].concat(schedule.reserved || [], schedule.favorites || []);
      if (all.length) conv.appendChild(window.Sessions.renderSessionList(all));
    }
    window.Sessions.refreshAllCards();
    scroll();
  }

  function showTypingIndicator() {
    const conv = el("conversation");
    const t = document.createElement("div");
    t.className = "msg msg-assistant typing";
    t.id = "typing";
    t.innerHTML = '<span class="msg-who">Assistant</span>' +
      '<div class="msg-body"><span class="dot"></span><span class="dot"></span><span class="dot"></span> Thinking…</div>';
    conv.appendChild(t);
    scroll();
  }
  function hideTypingIndicator() { const t = el("typing"); if (t) t.remove(); }

  function scroll() {
    const conv = el("conversation");
    conv.scrollTop = conv.scrollHeight;
  }

  function notify(text, isError) {
    const banner = el("global-error");
    banner.textContent = text;
    banner.hidden = false;
    banner.classList.toggle("is-error", !!isError);
    clearTimeout(notify._t);
    notify._t = setTimeout(() => { banner.hidden = true; }, 5000);
  }

  function setSending(on) {
    sending = on;
    el("send-btn").disabled = on;
    el("chat-input").disabled = on;
  }

  // Prominent, actionable reconnect prompt shown when the session has expired.
  // The deployed app uses the native/loopback connect model, so the fix is to
  // re-run the local connect; this banner tells the user exactly that and lets
  // them re-check status once they've reconnected.
  function showReconnectBanner() {
    const banner = el("global-error");
    if (!banner) return;
    banner.innerHTML = "";
    const msg = document.createElement("span");
    msg.textContent = "Your session expired. Reconnect by running " +
      "./scripts/connect.sh locally, then ";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-secondary reconnect-recheck";
    btn.textContent = "re-check connection";
    btn.addEventListener("click", () => {
      if (window.Auth) window.Auth.checkAuthStatus();
      banner.hidden = true;
    });
    banner.appendChild(msg);
    banner.appendChild(btn);
    banner.hidden = false;
    banner.classList.add("is-error");
    // This one persists (no auto-hide) until the user acts.
    clearTimeout(notify._t);
  }

  async function sendMessage(message) {
    const text = (message || "").trim();
    if (!text || sending) return;
    ensureConversation();
    hideSuggestionsIfNeeded();
    appendMessage("user", text);
    el("chat-input").value = "";
    autosize(el("chat-input"));
    setSending(true);
    showTypingIndicator();
    try {
      const { data } = await window.API.chat(conversationId, text);
      hideTypingIndicator();
      handleChatResponse(data);
    } catch (err) {
      hideTypingIndicator();
      handleChatError(err);
    } finally {
      setSending(false);
      el("chat-input").focus();
    }
  }

  function handleChatResponse(data) {
    if (!data) { notify("Something went wrong. Please try again.", true); return; }
    if (data.authState === "not_registered") {
      appendMessage("assistant", data.message ||
        "Your AWS identity is authenticated, but AWS Events does not authorize access to re:Invent 2026.");
      return;
    }
    appendMessage("assistant", data.message || "");
    if (data.sessions && data.sessions.length) {
      // Keep favorite-state cache in sync with any action just performed.
      appendSessions(data.sessions);
    }
    // Structured Schedule_View (from show_schedule): render it as cards here so
    // the assistant only has to write a short summary, not re-list every item.
    if (data.schedule) {
      appendSchedule(data.schedule);
    }
    const action = data.action;
    if (action) {
      const fav = action.type === "favorite" && action.status === "success";
      const unfav = action.type === "unfavorite" && action.status === "success";
      if (fav) window.Sessions.markFavorite(action.sessionId, true);
      if (unfav) window.Sessions.markFavorite(action.sessionId, false);
      // Reflect on any visible card for that session.
      syncCard(action.sessionId);
      if (action.status !== "success") {
        notify("I couldn't update your favorite. Please try again.", true);
      }
    }
  }

  function syncCard(sessionId) {
    if (!sessionId) return;
    document.querySelectorAll('.session-card[data-session-id="' + cssEscape(sessionId) + '"]')
      .forEach((card) => {
        const btn = card.querySelector(".fav-btn");
        if (!btn) return;
        const isFav = window.Sessions.isFavorite(sessionId);
        btn.classList.toggle("is-fav", isFav);
        btn.setAttribute("aria-pressed", isFav ? "true" : "false");
        btn.textContent = isFav ? "★ Favorited" : "☆ Favorite";
      });
  }

  function cssEscape(s) { return String(s).replace(/["\\]/g, "\\$&"); }

  function handleChatError(err) {
    if (err && err.category === "AUTH_REQUIRED") {
      showReconnectBanner();
      appendMessage("assistant",
        "Your session has expired. Please reconnect to continue.");
      if (window.Auth) window.Auth.checkAuthStatus();
    } else if (err && err.category === "NETWORK") {
      appendMessage("assistant",
        "Unable to reach the Session Assistant. Please check your connection and try again.");
    } else {
      appendMessage("assistant",
        "I couldn't retrieve re:Invent information right now. Please try again.");
    }
  }

  async function showFavoritesView() {
    ensureConversation();
    hideSuggestionsIfNeeded();
    appendMessage("user", "Show my favorites");
    setSending(true);
    showTypingIndicator();
    const sessions = await window.Favorites.loadFavorites();
    hideTypingIndicator();
    setSending(false);
    if (sessions === null) {
      appendMessage("assistant", "I couldn't retrieve your favorites right now. Please try again.");
      return;
    }
    if (!sessions.length) {
      appendMessage("assistant",
        "You don't have any favorite sessions yet. Try searching for sessions and selecting Favorite.");
      return;
    }
    appendMessage("assistant", "Here are your current favorites.");
    const list = window.Sessions.renderSessionList(sessions);
    list.querySelectorAll(".session-card").forEach((c) => (c.dataset.view = "favorites"));
    el("conversation").appendChild(list);
    scroll();
  }

  // Mirrors showFavoritesView for reservations: lists the owner's reserved
  // sessions from GET /reservations. Stamps each card with view="reserved" so
  // reservations.js removes the card when it is cancelled from this view.
  // Truthful empty/error states only — never fabricate reserved sessions.
  async function showReservationsView() {
    ensureConversation();
    hideSuggestionsIfNeeded();
    appendMessage("user", "Show my reserved sessions");
    setSending(true);
    showTypingIndicator();
    let data = null;
    try {
      ({ data } = await window.API.getReservations());
    } catch (_) {
      data = null;
    }
    hideTypingIndicator();
    setSending(false);
    if (!data) {
      appendMessage("assistant", "I couldn't retrieve your reserved sessions right now.");
      return;
    }
    const sessions = data.sessions || [];
    if (!sessions.length) {
      // `total` is the true number of reserved seats. An empty list with a
      // positive total means the details could not be loaded, which is NOT the
      // same as having no reservations, so say so honestly.
      appendMessage("assistant", data.total > 0 && data.message
        ? data.message
        : "You don't have any reserved sessions yet.");
      return;
    }
    appendMessage("assistant", data.message && data.total > sessions.length
      ? data.message
      : "Here are your reserved sessions.");
    const list = window.Sessions.renderSessionList(sessions);
    list.querySelectorAll(".session-card").forEach((c) => (c.dataset.view = "reserved"));
    el("conversation").appendChild(list);
    scroll();
  }

  function hideSuggestionsIfNeeded() {
    const intro = document.querySelector(".intro");
    if (intro) intro.classList.add("intro-collapsed");
  }

  function autosize(ta) {
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
  }

  function wire() {
    renderSuggestions();
    const form = el("chat-form");
    const input = el("chat-input");
    form.addEventListener("submit", (e) => { e.preventDefault(); sendMessage(input.value); });
    input.addEventListener("input", () => autosize(input));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage(input.value);
      }
    });
  }

  window.Chat = { wire, reset, sendMessage, notify, renderSuggestions, showFavoritesView, showReservationsView };
})();
