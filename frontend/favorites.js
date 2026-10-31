/* favorites.js — favorite/unfavorite via backend; update card state only on
 * confirmed success. Never assume a click means success. */
(function () {
  async function toggle(session, btn, card) {
    const id = session.sessionId;
    const currentlyFav = window.Sessions.isFavorite(id);
    setButtonBusy(btn, currentlyFav ? "Removing…" : "Saving…");
    try {
      const { data } = currentlyFav
        ? await window.API.unfavorite(id)
        : await window.API.favorite(id);
      const action = data && data.action;
      if (action && action.status === "success") {
        const nowFav = !currentlyFav;
        window.Sessions.markFavorite(id, nowFav);
        setButtonState(btn, nowFav);
        if (window.Chat) window.Chat.notify(nowFav
          ? "✓ Added to your favorites." : "Removed from your favorites.");
        // If we're in the favorites view, remove the card on unfavorite.
        if (!nowFav && card && card.dataset.view === "favorites") card.remove();
        // If My Schedule is open, reload it so it and the data "Add to
        // calendar" exports reflect the change (no-op when closed).
        if (window.ScheduleView && typeof window.ScheduleView.onScheduleChanged === "function") {
          window.ScheduleView.onScheduleChanged();
        }
      } else {
        setButtonState(btn, currentlyFav);
        if (window.Chat) window.Chat.notify(
          "I couldn't update your favorite. Please try again.", true);
      }
    } catch (err) {
      setButtonState(btn, currentlyFav);
      handleError(err);
    }
  }

  function setButtonBusy(btn, label) {
    btn.disabled = true;
    btn.dataset.prev = btn.textContent;
    btn.textContent = label;
  }

  function setButtonState(btn, isFav) {
    btn.disabled = false;
    btn.classList.toggle("is-fav", isFav);
    btn.setAttribute("aria-pressed", isFav ? "true" : "false");
    btn.textContent = isFav ? "★ Favorited" : "☆ Favorite";
  }

  async function loadFavoriteIds() {
    // Fast: fetch just the favorite id set (one GetSchedule call) to populate
    // the cache so cards render/refresh with the correct star state.
    try {
      const { data } = await window.API.favoriteIds();
      const ids = (data && data.favoriteIds) || [];
      window.Sessions.setFavoriteIds(ids);
      window.Sessions.refreshAllCards();
      return ids;
    } catch (err) {
      // Non-fatal: cards just show the empty star until a favorite action.
      return null;
    }
  }

  async function loadFavorites() {
    try {
      const { data } = await window.API.getFavorites();
      const sessions = (data && data.sessions) || [];
      window.Sessions.setFavoriteIds(sessions.map((s) => s.sessionId));
      return sessions;
    } catch (err) {
      handleError(err);
      return null;
    }
  }

  function handleError(err) {
    if (!window.Chat) return;
    if (err && err.category === "AUTH_REQUIRED") {
      window.Chat.notify("Your session has expired. Please reconnect.", true);
      if (window.Auth) window.Auth.checkAuthStatus();
    } else if (err && err.category === "NETWORK") {
      window.Chat.notify("Unable to reach the Session Assistant. Please check your connection.", true);
    } else {
      window.Chat.notify("Something went wrong. Please try again.", true);
    }
  }

  window.Favorites = { toggle, loadFavorites, loadFavoriteIds };
})();
