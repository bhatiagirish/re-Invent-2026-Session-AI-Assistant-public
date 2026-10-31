/* reservations.js — PHASED reservation controls (Req 21.7, 22.1, 23.5, 23.8).
 *
 * Mirrors favorites.js: reserve / cancel-reservation go through the application
 * backend, and the card is updated ONLY on a confirmed success
 * (action.status === "success"). On failure the card is left unchanged and a
 * non-blocking indication is shown.
 *
 * A reservation is NOT a favorite. The control wording is deliberately distinct
 * ("Reserve a seat" / "Cancel reservation" vs "Favorite"); this module never
 * relabels a favorite as reserved.
 *
 * Reservation controls are rendered only when the phased capability is enabled
 * (window.Auth.reservationsEnabled()). When the flag is OFF, no control is
 * rendered — enabled() short-circuits every render path. Reserved state is
 * sourced from the backend GetSchedule reserved[] array (empty until used).
 *
 * The browser talks ONLY to the application backend, never to AWS Events MCP.
 */
(function () {
  // Known reserved session ids, so controls render in the right state. Populated
  // from the Schedule_View reserved[] array; empty until the capability is used.
  var reservedIds = new Set();
  function setReservedIds(ids) {
    reservedIds.clear();
    (ids || []).forEach(function (i) { reservedIds.add(i); });
  }
  function markReserved(id, on) {
    if (on) reservedIds.add(id); else reservedIds.delete(id);
  }
  function isReserved(id) { return reservedIds.has(id); }

  // The single switch: controls exist iff the phased capability is enabled.
  function enabled() {
    return !!(window.Auth && typeof window.Auth.reservationsEnabled === "function" &&
      window.Auth.reservationsEnabled());
  }

  function esc(s) {
    if (window.Sessions && typeof window.Sessions.esc === "function") {
      return window.Sessions.esc(s);
    }
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function reserveLabel(reserved) {
    // State-only label mirroring the favorite button ("☆ Favorite" / "★
    // Favorited"): the button shows what IS, and its toggle semantics are
    // carried by aria-pressed + the is-reserved style. Wording stays distinct
    // from the favorite control (Req 9: favorite ≠ reservation).
    return reserved ? "✓ Reserved" : "Reserve a seat";
  }

  // Hover/tooltip text surfacing the implicit action, since the label is
  // state-only. Reserved -> clicking cancels; not reserved -> clicking reserves.
  function reserveTitle(reserved) {
    return reserved ? "Click to cancel your reservation" : "Reserve a seat";
  }

  // Build a reservation button for a session. Returns null when the flag is OFF
  // so callers render NO reservation control.
  //
  // Walk-up gate (Req: do not offer to reserve a non-reservable session): AWS
  // Events marks each session isReservable. Most breakout sessions are WALK-UP
  // ONLY (isReservable === false) and cannot be reserved, so we render NO reserve
  // toggle for them. Exception: if a session is ALREADY reserved we still show
  // the control (so the user can CANCEL a seat they somehow hold), and we only
  // suppress on an EXPLICIT false (missing/true -> show), so a sparse payload
  // never silently hides a reservable session's control.
  function isReservableSession(session) {
    return !(session && session.isReservable === false);
  }

  function buildButton(session) {
    if (!enabled()) return null;
    var id = session && session.sessionId;
    if (!id) return null;
    // Walk-up only AND not already reserved -> no reserve control at all.
    if (!isReservableSession(session) && !isReserved(id)) return null;
    var btn = document.createElement("button");
    btn.type = "button";
    var reserved = isReserved(id);
    btn.className = "btn reserve-btn" + (reserved ? " is-reserved" : "");
    btn.setAttribute("aria-pressed", reserved ? "true" : "false");
    btn.title = reserveTitle(reserved);
    btn.textContent = reserveLabel(reserved);
    btn.addEventListener("click", function () { toggle(session, btn); });
    return btn;
  }

  // Insert a reservation control into an already-rendered session card, after
  // the Favorite button. No-op when disabled or when a control already exists.
  function attachToCard(session, card) {
    if (!enabled() || !card) return null;
    var actions = card.querySelector(".card-actions");
    if (!actions || actions.querySelector(".reserve-btn")) return null;
    var btn = buildButton(session);
    if (btn) actions.appendChild(btn);
    return btn;
  }

  function setButtonBusy(btn, label) {
    btn.disabled = true;
    btn.dataset.prev = btn.textContent;
    btn.textContent = label;
  }

  function setButtonState(btn, reserved) {
    btn.disabled = false;
    btn.classList.toggle("is-reserved", reserved);
    btn.setAttribute("aria-pressed", reserved ? "true" : "false");
    btn.title = reserveTitle(reserved);
    btn.textContent = reserveLabel(reserved);
  }

  // Map a backend failure `reason` to a clear, friendly message for the user.
  // The reasons come from reservations.reserve_session / cancel_reservation
  // (session_full, schedule_conflict, not_confirmed, service_unavailable, ...).
  function reserveFailureMessage(action, wasReserved) {
    var reason = action && action.reason;
    if (wasReserved) {
      // A failed CANCEL.
      if (reason === "service_unavailable") {
        return "The reservation service is busy right now. Your seat is unchanged — please try again.";
      }
      return "I couldn't cancel your reservation. Please try again.";
    }
    // A failed RESERVE.
    switch (reason) {
      case "session_full":
        return "That session is full, so no seat was reserved. Walk-up seating may be available at the room.";
      case "schedule_conflict":
        return "That session clashes with something already on your schedule, so no seat was reserved.";
      case "not_confirmed":
        return "AWS Events didn't confirm a seat, so no reservation was made. Please try again or check the AWS Events catalog.";
      case "service_unavailable":
        return "The reservation service is busy right now. No seat was reserved — please try again.";
      default:
        return "I couldn't reserve that session. Please try again.";
    }
  }

  // Reserve or cancel for a session, updating the card ONLY on confirmed success.
  async function toggle(session, btn, card) {
    if (!enabled()) return;
    var id = session.sessionId;
    var currentlyReserved = isReserved(id);
    setButtonBusy(btn, currentlyReserved ? "Cancelling…" : "Reserving…");
    try {
      var res = currentlyReserved
        ? await window.API.cancelReservation(id)
        : await window.API.reserve(id);
      var action = res && res.data && res.data.action;
      if (action && action.status === "success") {
        var nowReserved = !currentlyReserved;
        markReserved(id, nowReserved);
        setButtonState(btn, nowReserved);
        // Honest success wording: distinguish a fresh action from an idempotent
        // one (already-reserved / not-currently-reserved) the backend reports.
        if (window.Chat) {
          var msg;
          if (nowReserved) {
            msg = action.reason === "already_reserved"
              ? "You already had a seat for this session."
              : "✓ Seat reserved.";
          } else {
            msg = action.reason === "not_currently_reserved"
              ? "You didn't have a seat for this session."
              : "Reservation cancelled.";
          }
          window.Chat.notify(msg);
        }
        // In the Schedule_View, drop the reserved card on cancel.
        if (!nowReserved && card && card.dataset &&
            card.dataset.view === "reserved") card.remove();
      } else {
        // Leave the card unchanged; show a SPECIFIC, friendly reason (full /
        // clash / unconfirmed / busy) from the backend action, not a generic
        // "try again" that hides why the reserve didn't happen.
        setButtonState(btn, currentlyReserved);
        if (window.Chat) window.Chat.notify(
          reserveFailureMessage(action, currentlyReserved), true);
      }
    } catch (err) {
      setButtonState(btn, currentlyReserved);
      handleError(err);
    }
  }

  function handleError(err) {
    if (!window.Chat) return;
    if (err && err.category === "AUTH_REQUIRED") {
      window.Chat.notify("Your session has expired. Please reconnect.", true);
      if (window.Auth) window.Auth.checkAuthStatus();
    } else if (err && err.category === "NETWORK") {
      window.Chat.notify(
        "Unable to reach the Session Assistant. Please check your connection.", true);
    } else {
      window.Chat.notify("Something went wrong. Please try again.", true);
    }
  }

  // Build a standalone "Cancel reservation" control for a reserved item in the
  // Schedule_View (Req 22.1). Returns null when the flag is OFF.
  function buildCancelControl(session) {
    if (!enabled()) return null;
    var id = session && session.sessionId;
    if (!id) return null;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-danger reserve-cancel-btn";
    btn.textContent = "Cancel reservation";
    btn.setAttribute("data-reserve-cancel", id);
    btn.addEventListener("click", function () {
      // Reserved items in the schedule are already reserved, so this always cancels.
      markReserved(id, true);
      toggle(session, btn, null);
    });
    return btn;
  }

  // Reconcile every rendered reservation control with the current reserved set,
  // parallel to Sessions.refreshAllCards() for favorites. The session id lives
  // on the enclosing .session-card's dataset.sessionId (buildButton sets no
  // per-button id attribute), so walk up to the card for the id.
  function refreshAllCards() {
    document.querySelectorAll(".reserve-btn").forEach(function (btn) {
      var card = btn.closest(".session-card");
      var id = card && card.dataset ? card.dataset.sessionId : null;
      if (!id) return;
      setButtonState(btn, isReserved(id));
    });
  }

  // Fast: fetch just the reserved id set to populate the cache so controls
  // render/refresh in the correct state. Mirrors Favorites.loadFavoriteIds().
  // No-op (issues NO request) when the phased capability is OFF (Req 8.2); the
  // caller in app.js also guards, but guarding here is correct and safe.
  async function loadReservedIds() {
    if (!enabled()) return;
    try {
      var res = await window.API.reservedIds();
      var ids = (res && res.data && res.data.reservedIds) || [];
      setReservedIds(ids);
      refreshAllCards();
      return ids;
    } catch (err) {
      // Non-fatal: leave cards in the default "Reserve a seat" state. The load
      // must not be blocked, so swallow the error and do not throw.
      return null;
    }
  }

  window.Reservations = {
    enabled,
    setReservedIds,
    markReserved,
    isReserved,
    toggle,
    buildButton,
    attachToCard,
    buildCancelControl,
    reserveLabel,
    refreshAllCards,
    loadReservedIds,
  };
})();
