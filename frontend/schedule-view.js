/* schedule-view.js — persistent "My Schedule" control + Schedule_View wiring
 * (Req 23.1).
 *
 * Provides a labeled control (the header "My Schedule" button) that opens the
 * Schedule_View without typing a chat message. The control is only usable while
 * the owner is authorized (authState === "authorized"); it stays hidden
 * otherwise. Activating it calls GET /schedule on the application backend and
 * renders the result via window.Schedule.renderScheduleView (built in 10.1).
 *
 * On a failed/non-2xx fetch the error state is rendered by passing
 * {error:true, message} to renderScheduleView — never a partial or cached view.
 *
 * This module also exposes the create/update/delete personal-time handlers that
 * app.js registers. The input form itself is task 10.3; these handlers accept an
 * already-prepared entry (or id) object, call the backend, and on confirmed
 * success refresh the Schedule_View so it reflects AWS Events state.
 *
 * The browser talks ONLY to the application backend, never to AWS Events MCP.
 */
(function () {
  function el(id) { return document.getElementById(id); }

  function els() {
    return {
      openBtn: el("schedule-btn"),
      backdrop: el("schedule-backdrop"),
      modal: el("schedule-modal"),
      closeBtn: el("schedule-close"),
      content: el("schedule-content"),
      addBtn: el("schedule-add-btn"),
      formSlot: el("schedule-form-slot"),
      exportBtn: el("schedule-export-btn"),
      exportSlot: el("schedule-export-slot"),
    };
  }

  // ── "Add to calendar" (.ics export, frontend/calendar.js) ──────────────────
  // The export uses the schedule the dialog last loaded successfully, so it
  // needs no second request. It is unavailable while nothing (or only an error)
  // is shown.
  let loadedSchedule = null;

  function setLoadedSchedule(data) {
    loadedSchedule = data || null;
    const e = els();
    if (e.exportBtn) e.exportBtn.disabled = !loadedSchedule;
  }

  function hideExport() {
    const e = els();
    if (e.exportSlot) { e.exportSlot.hidden = true; e.exportSlot.innerHTML = ""; }
    if (e.exportBtn) e.exportBtn.setAttribute("aria-expanded", "false");
  }

  function openExport() {
    const e = els();
    if (!e.exportSlot || !loadedSchedule || !window.Calendar ||
        typeof window.Calendar.openPanel !== "function") {
      return;
    }
    if (!e.exportSlot.hidden) { hideExport(); return; }     // the button toggles
    e.exportSlot.hidden = false;
    if (e.exportBtn) e.exportBtn.setAttribute("aria-expanded", "true");
    window.Calendar.openPanel(e.exportSlot, { data: loadedSchedule, onClose: hideExport });
  }

  // Show/hide the persistent control based on authorization. Called by app.js
  // after every auth-status check so the control appears exactly in State 3.
  function setAuthorized(authorized) {
    const e = els();
    if (e.openBtn) e.openBtn.hidden = !authorized;
    if (!authorized) closeView();
  }

  function renderInto(node) {
    const e = els();
    if (!e.content) return;
    e.content.innerHTML = "";
    e.content.appendChild(node);
  }

  function showLoading() {
    const e = els();
    if (!e.content) return;
    const div = document.createElement("div");
    div.className = "schedule-state schedule-loading";
    div.innerHTML = '<p class="schedule-state-title">Loading your schedule…</p>';
    renderInto(div);
  }

  function openModal() {
    const e = els();
    if (e.backdrop) e.backdrop.hidden = false;
    if (e.closeBtn) e.closeBtn.focus();
  }

  function closeView() {
    const e = els();
    if (e.backdrop) e.backdrop.hidden = true;
    hideForm();
    hideExport();
  }

  // Fetch GET /schedule and render it. On any failure render the error state via
  // renderScheduleView({error:true,...}) so no partial/cached view is shown.
  function delay(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  // While the catalog index is still being built (about a minute after a
  // deploy, or after a long absence), favorites have no fast source for their
  // details, so loading now would take the slow per-session path that tends to
  // fail. Wait for the index instead (at most INDEX_WAIT_MS), saying why. If it
  // is not ready by then, load anyway: the slow path still works, just slowly.
  const INDEX_WAIT_MS = 45000;

  function showPreparing() {
    const e = els();
    if (!e.content) return;
    const div = document.createElement("div");
    div.className = "schedule-state schedule-loading schedule-preparing";
    div.setAttribute("role", "status");
    div.innerHTML =
      '<p class="schedule-state-title">Preparing your schedule…</p>' +
      '<p class="schedule-state-body">Getting the session index ready so your full ' +
      "schedule loads quickly. This can take about a minute.</p>";
    renderInto(div);
  }

  // Opening the dialog right after page load usually catches the first index
  // check still in flight. That is not a build (the index normally exists), so
  // show the ordinary "Loading" text and wait briefly for the answer; only if
  // the backend then says a build is under way, show "Preparing" and wait for it.
  const INDEX_CHECK_WAIT_MS = 10000;

  async function waitForIndex() {
    const C = window.Catalog;
    if (!C || typeof C.isBuilding !== "function" || typeof C.whenReady !== "function") {
      return;
    }
    const checking = () => typeof C.isChecking === "function" && C.isChecking();
    if (checking() && typeof C.whenKnown === "function") {
      showLoading();
      await C.whenKnown(INDEX_CHECK_WAIT_MS);
    }
    // Still no answer after the check window: a ready index answers within a
    // second or two, so a missing answer means a build step is running. Keep
    // waiting for it rather than falling back to the slow path (which is what
    // produced "Showing 47 of 70 favorites" right after a fresh deploy).
    if (!C.isBuilding() && !checking()) return;
    showPreparing();
    await C.whenReady(INDEX_WAIT_MS);
  }
  // Pauses before the 1st and 2nd automatic retry. A brief AWS Events hiccup or
  // throttle usually clears within a few seconds, so the pauses lengthen.
  const RETRY_DELAYS_MS = [2000, 4000];
  // A failure that took this long (a timeout) is not retried automatically: it
  // already cost most of a minute, and repeating it would only keep the user
  // staring at a spinner. The error box's "Try again" button covers that case.
  const SLOW_FAILURE_MS = 8000;

  // Load the schedule. A failed load is retried automatically (up to twice)
  // when it failed QUICKLY. An expired session (AUTH_REQUIRED) is never retried:
  // it cannot succeed until the user reconnects. Whatever still fails shows an
  // error with a "Try again" button.
  // `opts.skipIndexWait`: used by the refresh that follows adding, editing or
  // deleting personal time, so the form never sits on "Preparing" for up to
  // 45 s right after a save. Opening the dialog and "Try again" do wait.
  async function refresh(opts) {
    if (!window.Schedule || typeof window.Schedule.renderScheduleView !== "function") {
      return;
    }
    // An open export panel would hold the previous schedule; close it, and
    // disable export until this load succeeds.
    hideExport();
    setLoadedSchedule(null);
    if (!(opts && opts.skipIndexWait)) await waitForIndex();
    showLoading();
    let lastErr = null;
    let lastData = null;       // a backend "couldn't retrieve" response (HTTP 200)
    for (let attempt = 0; ; attempt++) {
      const startedAt = Date.now();
      lastErr = null;
      lastData = null;
      try {
        const { data } = await window.API.getSchedule();
        const degraded = data && typeof data.message === "string" && data.message;
        if (!degraded) {
          renderInto(window.Schedule.renderScheduleView(data || {}, { onRetry: refresh }));
          setLoadedSchedule(data || {});
          return;
        }
        lastData = data;
      } catch (err) {
        lastErr = err;
        if (err && err.category === "AUTH_REQUIRED") break;
      }
      const quick = Date.now() - startedAt < SLOW_FAILURE_MS;
      if (!quick || attempt >= RETRY_DELAYS_MS.length) break;
      await delay(RETRY_DELAYS_MS[attempt]);
    }
    if (lastData) {
      // Show the backend's own message, with a Try again button.
      renderInto(window.Schedule.renderScheduleView(lastData, { onRetry: refresh }));
      return;
    }
    if (lastErr && lastErr.category === "AUTH_REQUIRED" && window.Auth) {
      window.Auth.checkAuthStatus();
    }
    const message = (lastErr && lastErr.category === "NETWORK")
      ? "Unable to reach the Session Assistant. Please check your connection."
      : "Please try again.";
    renderInto(window.Schedule.renderScheduleView(null,
      { error: true, message, onRetry: refresh }));
  }

  // Open the Schedule_View. Guarded by authorization so it never surfaces MCP
  // state for an unauthorized session.
  async function open() {
    if (window.Auth && typeof window.Auth.isAuthorized === "function" &&
        !window.Auth.isAuthorized()) {
      return;
    }
    openModal();
    await refresh();
  }

  // ── Personal-time handlers (registered by app.js) ──────────────────────────
  // The entry/id payloads are prepared by the form (task 10.3). Each handler
  // calls the backend, refreshes the view only on confirmed success, and returns
  // the ActionResult-style object so the caller (form) can keep entered values
  // and surface a non-blocking error on failure. State is left unchanged on
  // failure/timeout.
  async function createPersonalTime(entry) {
    const { data } = await window.API.createPersonalTime(entry);
    const action = data && data.action;
    if (action && action.status === "success") await refresh({ skipIndexWait: true });
    return action || { status: "failed" };
  }

  async function updatePersonalTime(personalTimeId, entry) {
    const { data } = await window.API.updatePersonalTime(personalTimeId, entry);
    const action = data && data.action;
    if (action && action.status === "success") await refresh({ skipIndexWait: true });
    return action || { status: "failed" };
  }

  async function deletePersonalTime(personalTimeId, card) {
    let data;
    try {
      ({ data } = await window.API.deletePersonalTime(personalTimeId));
    } catch (err) {
      if (window.Chat) window.Chat.notify(
        "The personal time block couldn't be deleted. Please try again.", true);
      return { status: "failed" };
    }
    const action = data && data.action;
    if (action && action.status === "success") {
      // Remove the exact card the user clicked (it may be in the chat
      // conversation, NOT the modal), then also refresh the modal if it's open
      // so both views stay consistent.
      if (card && typeof card.remove === "function") card.remove();
      const e = els();
      if (e.backdrop && !e.backdrop.hidden) await refresh({ skipIndexWait: true });
      if (window.Chat) window.Chat.notify("Personal time block deleted.");
    } else {
      if (window.Chat) window.Chat.notify(
        (action && action.message) ||
        "The personal time block couldn't be deleted. Please try again.", true);
    }
    return action || { status: "failed" };
  }

  // ── Personal-time form (task 10.3) ─────────────────────────────────────────
  // The add/edit form (window.PersonalTimeForm) renders into #schedule-form-slot
  // above the list. On a confirmed success the handlers already refresh the
  // view (ScheduleView.create/update/deletePersonalTime), so onDone just closes
  // the form; on failure the form stays open and populated for retry.
  function hideForm() {
    const e = els();
    if (e.formSlot) { e.formSlot.hidden = true; e.formSlot.innerHTML = ""; }
    if (e.addBtn) e.addBtn.disabled = false;
  }

  function openForm(mode, item) {
    if (!window.PersonalTimeForm || typeof window.PersonalTimeForm.open !== "function") {
      return;
    }
    const e = els();
    if (!e.formSlot) return;
    if (e.addBtn) e.addBtn.disabled = true;
    e.formSlot.hidden = false;
    window.PersonalTimeForm.open(e.formSlot, {
      mode: mode,
      item: item,
      onDone: hideForm,     // refresh already happened on success
      onCancel: hideForm,
    });
  }

  // Build the item payload for the edit form from a personal-time card's
  // data-attrs (stashed by schedule.js, Pacific-local for start/end).
  function editItemFromCard(card) {
    if (!card || !card.dataset) return null;
    return {
      personalTimeId: card.dataset.personalTimeId || "",
      title: card.dataset.title || "",
      start: card.dataset.startLocal || "",
      end: card.dataset.endLocal || "",
      location: card.dataset.location || "",
      description: card.dataset.description || "",
    };
  }

  // Event delegation for Edit / Delete buttons on personal-time cards. The
  // Personal-time cards are rendered BOTH in the My Schedule modal and inline in
  // the chat conversation, and are re-rendered on every refresh. So we delegate
  // from DOCUMENT (not just the modal container) and match the Edit/Delete
  // button via closest(), so the controls work wherever the card appears.
  function onPersonalTimeClick(ev) {
    const t = ev && ev.target;
    if (!t || typeof t.closest !== "function") return;
    const editBtn = t.closest("[data-pt-edit]");
    if (editBtn) {
      const card = closestCard(editBtn);
      const item = editItemFromCard(card);
      if (item) {
        openModalIfClosed();   // edit form lives in the modal; open it if needed
        openForm("edit", item);
      }
      return;
    }
    const delBtn = t.closest("[data-pt-delete]");
    if (delBtn) {
      const id = delBtn.getAttribute("data-pt-delete");
      const card = closestCard(delBtn);
      if (id) deletePersonalTime(id, card);
    }
  }

  // The edit form renders into the modal's #schedule-form-slot, so if the user
  // clicked Edit on a chat-rendered card (modal closed), open the modal first.
  function openModalIfClosed() {
    const e = els();
    if (e.backdrop && e.backdrop.hidden) openModal();
  }

  // Walk up to the enclosing personal-time card (carries data-personal-time-id).
  function closestCard(node) {
    let n = node;
    while (n) {
      if (n.dataset && n.dataset.personalTimeId != null && n.dataset.personalTimeId !== undefined) {
        if (n.classList && n.classList.contains("kind-personal")) return n;
      }
      n = n.parentNode;
    }
    return null;
  }

  function wire() {
    const e = els();
    if (e.openBtn) e.openBtn.addEventListener("click", open);
    if (e.closeBtn) e.closeBtn.addEventListener("click", closeView);
    if (e.addBtn) e.addBtn.addEventListener("click", () => openForm("add", null));
    // "Add to calendar" stays disabled until a schedule has loaded.
    if (e.exportBtn) {
      e.exportBtn.disabled = !loadedSchedule;
      e.exportBtn.addEventListener("click", openExport);
    }
    // Delegate from document so Edit/Delete work on personal-time cards whether
    // they are shown in the modal or inline in the chat conversation.
    document.addEventListener("click", onPersonalTimeClick);
    if (e.backdrop) {
      e.backdrop.addEventListener("click", (ev) => {
        if (ev.target === e.backdrop) closeView();
      });
    }
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && e.backdrop && !e.backdrop.hidden) closeView();
    });
  }

  window.ScheduleView = {
    wire,
    setAuthorized,
    open,
    refresh,
    close: closeView,
    createPersonalTime,
    updatePersonalTime,
    deletePersonalTime,
    // Exposed for the add/edit controls and unit tests (10.3 / 10.6).
    openForm,
    hideForm,
    openExport,
    hideExport,
  };
})();
