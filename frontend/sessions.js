/* sessions.js — render session cards and the details modal.
 * Only fields actually present in the backend response are shown (no fabrication). */
(function () {
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // Track known favorite ids so buttons render in the right state.
  const favoriteIds = new Set();
  function setFavoriteIds(ids) { favoriteIds.clear(); (ids || []).forEach((i) => favoriteIds.add(i)); }
  function markFavorite(id, on) { if (on) favoriteIds.add(id); else favoriteIds.delete(id); }
  function isFavorite(id) { return favoriteIds.has(id); }

  function timeLine(s) {
    const t = s.sessionTime || {};
    const bits = [t.date, t.time].filter(Boolean).join(" ");
    return bits || null;
  }

  function metaRow(label, value) {
    if (!value) return "";
    return '<div class="card-meta"><span class="meta-label">' + esc(label) +
      '</span> <span>' + esc(value) + "</span></div>";
  }

  function renderSessionCard(s) {
    const card = document.createElement("article");
    card.className = "session-card";
    card.dataset.sessionId = s.sessionId || "";

    const fav = isFavorite(s.sessionId);
    const when = timeLine(s);
    const speakers = (s.speakers || []).join(", ");

    card.innerHTML =
      '<div class="card-head">' +
        (s.sessionCode ? '<span class="card-code">' + esc(s.sessionCode) + "</span>" : "") +
        (s.type ? '<span class="card-type">' + esc(s.type) + "</span>" : "") +
      "</div>" +
      '<h3 class="card-title">' + esc(s.title || "Untitled session") + "</h3>" +
      metaRow("Level", s.level) +
      metaRow("When", when) +
      metaRow("Where", [s.venue, s.room].filter(Boolean).join(" · ")) +
      metaRow("Speakers", speakers) +
      '<div class="card-actions">' +
        '<button type="button" class="btn btn-ghost details-btn">More Details</button>' +
        '<button type="button" class="btn fav-btn ' + (fav ? "is-fav" : "") + '" ' +
          'aria-pressed="' + (fav ? "true" : "false") + '">' +
          (fav ? "★ Favorited" : "☆ Favorite") +
        "</button>" +
      "</div>";

    card.querySelector(".details-btn").addEventListener("click", () => showSessionDetails(s));
    const favBtn = card.querySelector(".fav-btn");
    favBtn.addEventListener("click", () => window.Favorites.toggle(s, favBtn, card));

    // Phased reservation control (Req 21.7, 23.8). Rendered alongside Favorite
    // ONLY when the capability is enabled; when the flag is OFF, attachToCard is
    // a no-op and no reservation control appears. A reservation is NOT a
    // favorite — the control carries its own distinct seat/reservation wording.
    if (window.Reservations && typeof window.Reservations.attachToCard === "function") {
      window.Reservations.attachToCard(s, card);
    }
    return card;
  }

  function renderSessionList(sessions) {
    const wrap = document.createElement("div");
    wrap.className = "session-list";
    (sessions || []).forEach((s) => wrap.appendChild(renderSessionCard(s)));
    return wrap;
  }

  function detailRow(label, value) {
    if (!value) return "";
    return "<p><strong>" + esc(label) + ":</strong> " + esc(value) + "</p>";
  }

  function showSessionDetails(s) {
    const backdrop = document.getElementById("details-backdrop");
    const content = document.getElementById("details-content");
    const when = timeLine(s);
    content.innerHTML =
      '<h2 id="details-title">' + esc(s.title || "Session") + "</h2>" +
      (s.sessionCode ? '<p class="details-code">' + esc(s.sessionCode) + "</p>" : "") +
      detailRow("Type", s.type) +
      detailRow("Level", s.level) +
      detailRow("When", when) +
      detailRow("Venue", s.venue) +
      detailRow("Room", s.room) +
      detailRow("Services", (s.services || []).join(", ")) +
      detailRow("Topics", (s.topics || []).join(", ")) +
      detailRow("Speakers", (s.speakers || []).join(", ")) +
      (s.abstract ? '<div class="details-abstract"><strong>Abstract</strong><p>' +
        esc(s.abstract) + "</p></div>" : "");
    backdrop.hidden = false;
    document.getElementById("details-close").focus();
  }

  function closeDetails() {
    document.getElementById("details-backdrop").hidden = true;
  }

  function refreshAllCards() {
    document.querySelectorAll(".session-card").forEach((card) => {
      const id = card.dataset.sessionId;
      const btn = card.querySelector(".fav-btn");
      if (!id || !btn) return;
      const fav = isFavorite(id);
      btn.classList.toggle("is-fav", fav);
      btn.setAttribute("aria-pressed", fav ? "true" : "false");
      btn.textContent = fav ? "\u2605 Favorited" : "\u2606 Favorite";
    });
  }

  window.Sessions = {
    renderSessionCard, renderSessionList, showSessionDetails, closeDetails,
    setFavoriteIds, markFavorite, isFavorite, esc, refreshAllCards,
  };
})();
