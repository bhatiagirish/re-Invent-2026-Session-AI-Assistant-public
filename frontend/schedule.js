/* schedule.js — render the Schedule_View (Req 17, 22, 23.2).
 *
 * Shows the owner's reserved sessions, favorites, and personal-time blocks
 * together in one chronological list, each item carrying exactly ONE kind
 * label (reserved session / favorite / personal time). Every rendered item is
 * derived solely from the backend GET /schedule response — nothing is
 * fabricated. The browser talks only to the application backend, never to AWS
 * Events MCP.
 *
 * Backend response shape (backend/app.py get_schedule, models.py
 * ScheduleView/PersonalTimeEntry.to_public_dict):
 *   {
 *     reserved:     [ Session, ... ],   // reuse Session shape
 *     favorites:    [ Session, ... ],   // reuse Session shape
 *     personalTime: [ { personalTimeId, title, start, end, description?, location? }, ... ],
 *     message?:     "…"                 // present only on timeout/unavailable
 *   }
 * Personal-time start/end are the UTC contract form "YYYY-MM-DDTHH:MM:SS"
 * (no trailing Z, no offset); we convert them to Pacific event-local time for
 * display, including the zone designation (Req 17.4).
 */
(function () {
  var EVENT_TZ = "America/Los_Angeles";

  // Reuse the shared HTML-escaper so values are never injected as markup.
  function esc(s) {
    if (window.Sessions && typeof window.Sessions.esc === "function") {
      return window.Sessions.esc(s);
    }
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // Kind metadata: label text + CSS modifier class. Exactly one per item (Req 17.3).
  var KINDS = {
    reserved:  { label: "Reserved session", cls: "kind-reserved" },
    favorite:  { label: "Favorite",         cls: "kind-favorite" },
    personal:  { label: "Personal time",    cls: "kind-personal" },
  };

  // ── Pacific time formatting ────────────────────────────────────────────────
  // Parse a UTC contract datetime "YYYY-MM-DDTHH:MM:SS" (no Z) into a Date.
  function parseUtcContract(s) {
    if (!s || typeof s !== "string") return null;
    var m = s.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/);
    if (!m) return null;
    var d = new Date(Date.UTC(
      +m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)
    ));
    return isNaN(d.getTime()) ? null : d;
  }

  // Format a Date in Pacific event-local time, e.g. "Mon, Dec 1, 8:30 AM PST".
  // The short time-zone name (PST/PDT) is included to satisfy Req 17.4.
  function fmtPacific(date) {
    if (!date) return null;
    try {
      return new Intl.DateTimeFormat("en-US", {
        timeZone: EVENT_TZ,
        weekday: "short", month: "short", day: "numeric",
        hour: "numeric", minute: "2-digit",
        timeZoneName: "short",
      }).format(date);
    } catch (_) {
      // Fallback if Intl/timeZone is unavailable: label explicitly as Pacific.
      return date.toUTCString().replace("GMT", "UTC") + " (shown as Pacific)";
    }
  }

  // Convert a UTC contract datetime "YYYY-MM-DDTHH:MM:SS" into the Pacific
  // event-local "YYYY-MM-DDTHH:MM" form a datetime-local input expects, so the
  // edit form (task 10.3) pre-fills with the owner's original Pacific values.
  function toPacificLocalInput(s) {
    var d = parseUtcContract(s);
    if (!d) return "";
    try {
      var parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: EVENT_TZ, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false,
      }).formatToParts(d).reduce(function (acc, p) { acc[p.type] = p.value; return acc; }, {});
      var hour = parts.hour === "24" ? "00" : parts.hour; // normalize midnight
      return parts.year + "-" + parts.month + "-" + parts.day + "T" + hour + ":" + parts.minute;
    } catch (_) {
      return "";
    }
  }

  // ── Pacific -> UTC for sessions ─────────────────────────────────────────────
  // Sessions arrive as Pacific event-local "YYYY-MM-DD" + "HH:MM". Convert them
  // with the real IANA rule through Intl. (Never Date.parse: it reads the
  // BROWSER's time zone, so sessions sorted wrongly against personal time
  // outside Pacific, and Safari cannot parse "YYYY-MM-DD HH:MM" at all, which
  // pushed every session to the end of the list.)
  var pacificPartsFmt = null;
  function pacificParts(ms) {
    if (!pacificPartsFmt) {
      pacificPartsFmt = new Intl.DateTimeFormat("en-US", {
        timeZone: EVENT_TZ, hourCycle: "h23",
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
      });
    }
    var out = {};
    pacificPartsFmt.formatToParts(new Date(ms)).forEach(function (p) {
      if (p.type !== "literal") out[p.type] = parseInt(p.value, 10);
    });
    if (out.hour === 24) out.hour = 0;
    return out;
  }

  // Minutes Pacific is BEHIND UTC at a given instant (480 in PST, 420 in PDT).
  function pacificOffsetMinutes(ms) {
    try {
      var p = pacificParts(ms);
      var asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
      return Math.round((Math.floor(ms / 1000) * 1000 - asUtc) / 60000);
    } catch (_) {
      return 480;   // no Intl time zones: re:Invent week is PST (UTC-8)
    }
  }

  // Pacific "YYYY-MM-DD" + "HH:MM" -> UTC epoch milliseconds, or null.
  function pacificToUtcMs(date, time) {
    var d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(date || ""));
    var t = /^(\d{1,2}):(\d{2})/.exec(String(time || ""));
    if (!d || !t) return null;
    var guess = Date.UTC(+d[1], +d[2] - 1, +d[3], +t[1], +t[2]);
    var first = guess + pacificOffsetMinutes(guess) * 60000;
    return guess + pacificOffsetMinutes(first) * 60000;
  }

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  // The Pacific calendar day ("YYYY-MM-DD") of a UTC instant.
  function pacificDateKey(ms) {
    try {
      var p = pacificParts(ms);
      return p.year + "-" + pad2(p.month) + "-" + pad2(p.day);
    } catch (_) {
      var d = new Date(ms - 480 * 60000);
      return d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" + pad2(d.getUTCDate());
    }
  }

  // Start / end of a session as UTC epoch ms (null when not known).
  function sessionStartMs(s) {
    var t = (s && s.sessionTime) || {};
    return pacificToUtcMs(t.date, t.time);
  }

  function sessionEndMs(s) {
    var start = sessionStartMs(s);
    var len = Number(((s && s.sessionTime) || {}).length);
    return start != null && len > 0 ? start + len * 60000 : null;
  }

  // "Tuesday, Dec 1" for a "YYYY-MM-DD" day key.
  function fmtDayHeading(key) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(key || ""));
    if (!m) return String(key || "");
    try {
      return new Intl.DateTimeFormat("en-US", {
        timeZone: "UTC", weekday: "long", month: "short", day: "numeric",
      }).format(new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], 12)));
    } catch (_) {
      return key;
    }
  }

  // Build the "when" line for a personal-time entry (UTC contract -> Pacific).
  function personalWhen(p) {
    var start = parseUtcContract(p.start);
    var end = parseUtcContract(p.end);
    var sTxt = fmtPacific(start);
    var eTxt = fmtPacific(end);
    if (sTxt && eTxt) return sTxt + " – " + eTxt;
    return sTxt || eTxt || null;
  }

  // Build the "when" line for a session. Session times arrive as pre-formatted
  // display strings (sessionTime.date / sessionTime.time) rather than UTC
  // contract datetimes, so we show them verbatim and append the PT zone
  // designation (Req 17.4) without re-interpreting the values.
  function sessionWhen(s) {
    var t = s.sessionTime || {};
    var bits = [t.date, t.time].filter(Boolean).join(" ");
    if (!bits) return null;
    return bits + " PT";
  }

  // ── Ordering ────────────────────────────────────────────────────────────────
  // The three arrays each arrive ordered from the backend, but they must be
  // merged into one chronological list. We derive a comparable sort key per item
  // and apply the Req 17.2 tie-break: start asc, then end asc, then title
  // case-insensitive alphabetical. Items with no derivable start sort last but
  // keep a stable relative order so nothing is dropped (no fabrication).
  function startKey(item) {
    if (item.kind === "personal") {
      var d = parseUtcContract(item.data.start);
      return d ? d.getTime() : null;
    }
    return sessionStartMs(item.data);
  }

  function endKey(item) {
    if (item.kind === "personal") {
      var d = parseUtcContract(item.data.end);
      return d ? d.getTime() : null;
    }
    return sessionEndMs(item.data);
  }

  // The Pacific day an item belongs to ("YYYY-MM-DD"), or null when unknown.
  function itemDayKey(item) {
    if (item.kind === "personal") {
      var d = parseUtcContract(item.data.start);
      return d ? pacificDateKey(d.getTime()) : null;
    }
    var t = item.data.sessionTime || {};
    return /^\d{4}-\d{2}-\d{2}$/.test(t.date || "") ? t.date : null;
  }

  // All schedule items, tagged with exactly one kind and in display order.
  function scheduleItems(data) {
    data = data || {};
    var items = [];
    (Array.isArray(data.reserved) ? data.reserved : []).forEach(function (s) {
      items.push({ kind: "reserved", data: s });
    });
    (Array.isArray(data.favorites) ? data.favorites : []).forEach(function (s) {
      items.push({ kind: "favorite", data: s });
    });
    (Array.isArray(data.personalTime) ? data.personalTime : []).forEach(function (p) {
      items.push({ kind: "personal", data: p });
    });
    items.sort(compareItems);
    return items;
  }

  // Group ordered items by Pacific day: days in calendar order, then one group
  // for items whose day is unknown. Order inside a day is kept.
  function groupByDay(items) {
    var byKey = {};
    var keys = [];
    var undated = [];
    items.forEach(function (item) {
      var k = itemDayKey(item);
      if (!k) { undated.push(item); return; }
      if (!byKey[k]) { byKey[k] = []; keys.push(k); }
      byKey[k].push(item);
    });
    keys.sort();
    var groups = keys.map(function (k) { return { key: k, items: byKey[k] }; });
    if (undated.length) groups.push({ key: null, items: undated });
    return groups;
  }

  function titleKey(item) {
    return String(item.data.title || "").toLowerCase();
  }

  function compareItems(a, b) {
    var sa = startKey(a), sb = startKey(b);
    if (sa == null && sb != null) return 1;   // undated sort after dated
    if (sa != null && sb == null) return -1;
    if (sa != null && sb != null && sa !== sb) return sa - sb;

    var ea = endKey(a), eb = endKey(b);
    if (ea != null && eb != null && ea !== eb) return ea - eb;

    var ta = titleKey(a), tb = titleKey(b);
    if (ta < tb) return -1;
    if (ta > tb) return 1;
    return 0;
  }

  // ── Item rendering ───────────────────────────────────────────────────────────
  function metaRow(label, value) {
    if (!value) return "";
    return '<div class="card-meta"><span class="meta-label">' + esc(label) +
      '</span> <span>' + esc(value) + "</span></div>";
  }

  function kindBadge(kind) {
    var k = KINDS[kind] || { label: kind, cls: "" };
    return '<span class="kind-badge ' + k.cls + '">' + esc(k.label) + "</span>";
  }

  // A reserved session or favorite row. Reuses the shared session-card renderer
  // when available so cards look identical to search results, then stamps the
  // single kind badge on top.
  function renderSessionItem(item) {
    var card;
    if (window.Sessions && typeof window.Sessions.renderSessionCard === "function") {
      card = window.Sessions.renderSessionCard(item.data);
    } else {
      card = document.createElement("article");
      card.className = "session-card";
      card.innerHTML =
        '<h3 class="card-title">' + esc(item.data.title || "Untitled session") + "</h3>" +
        metaRow("When", sessionWhen(item.data)) +
        metaRow("Where", [item.data.venue, item.data.room].filter(Boolean).join(" · "));
    }
    card.classList.add("schedule-item");
    var k = KINDS[item.kind];
    if (k) card.classList.add(k.cls);
    var head = card.querySelector(".card-head");
    var badge = document.createElement("span");
    badge.className = "kind-badge " + (k ? k.cls : "");
    badge.textContent = k ? k.label : item.kind;
    if (head) head.insertBefore(badge, head.firstChild);
    else card.insertBefore(badge, card.firstChild);

    // Phased reservation control on reserved items (Req 22.1): a reserved item
    // carries a Cancel-reservation control ONLY when the capability is enabled,
    // mirroring the personal-time edit/delete delegation. When the flag is OFF,
    // buildCancelControl returns null and no control is added. The reserved[]
    // array is empty until the capability is used, so this is a no-op otherwise.
    if (item.kind === "reserved" && window.Reservations &&
        typeof window.Reservations.buildCancelControl === "function") {
      if (card.dataset) card.dataset.view = "reserved";
      // The shared session-card renderer also adds the per-card reserve TOGGLE
      // ("✓ Reserved", which also cancels on click). In the Schedule_View that
      // redundant with the dedicated Cancel-reservation control below (and the
      // "Reserved session" kind badge already shows status), so drop the toggle
      // here to leave a single, clear cancel affordance. Search-result cards are
      // unaffected (they keep the toggle as their only control).
      var toggle = card.querySelector(".reserve-btn");
      if (toggle) {
        if (typeof toggle.remove === "function") toggle.remove();
        else if (toggle.parentNode && typeof toggle.parentNode.removeChild === "function") {
          toggle.parentNode.removeChild(toggle);
        }
      }
      var cancelBtn = window.Reservations.buildCancelControl(item.data);
      if (cancelBtn) {
        // buildCancelControl is the single, shared Cancel_Control (Req 9.4) and
        // is reused as-is: we do NOT build a parallel control. Its own click
        // handler, however, calls toggle(session, btn, null) with no card, so
        // the Schedule_View card would never be removed on a confirmed cancel
        // (Req 9.2). Rewire the SAME control to forward this reserved card to
        // toggle so a confirmed success removes it (and a non-success leaves it
        // in place, per Req 9.3). cloneNode(true) yields a listener-free copy of
        // the exact same control, so there is one handler, not a duplicate.
        var wired = typeof cancelBtn.cloneNode === "function"
          ? cancelBtn.cloneNode(true)
          : cancelBtn;
        wired.addEventListener("click", function () {
          // Reserved schedule items are already reserved, so this always cancels.
          window.Reservations.markReserved(item.data.sessionId, true);
          window.Reservations.toggle(item.data, wired, card);
        });
        var actions = card.querySelector(".card-actions");
        if (!actions) {
          actions = document.createElement("div");
          actions.className = "card-actions reserve-cancel-actions";
          card.appendChild(actions);
        }
        actions.appendChild(wired);
      }
    }
    return card;
  }

  // A personal-time row. Not a catalog session and not a reservation (Req 18.7):
  // it carries only the personal-time kind badge and its own fields.
  function renderPersonalItem(item) {
    var p = item.data;
    var card = document.createElement("article");
    card.className = "session-card schedule-item " + KINDS.personal.cls;
    card.dataset.personalTimeId = p.personalTimeId || "";
    card.innerHTML =
      '<div class="card-head">' + kindBadge("personal") + "</div>" +
      '<h3 class="card-title">' + esc(p.title || "Personal time") + "</h3>" +
      metaRow("When", personalWhen(p)) +
      metaRow("Where", p.location) +
      (p.description
        ? '<div class="card-meta pt-description">' + esc(p.description) + "</div>"
        : "") +
      // Edit / Delete controls (Req 23.4). schedule-view.js wires these via
      // event delegation; the Pacific-local start/end are stashed as data-attrs
      // so the edit form pre-fills with the owner's original Pacific values.
      '<div class="card-actions pt-card-actions">' +
      '<button type="button" class="btn btn-secondary pt-edit-btn"' +
      ' data-pt-edit="' + esc(p.personalTimeId || "") + '">Edit</button>' +
      '<button type="button" class="btn btn-danger pt-delete-btn"' +
      ' data-pt-delete="' + esc(p.personalTimeId || "") + '">Delete</button>' +
      "</div>";
    card.dataset.title = p.title || "";
    card.dataset.location = p.location || "";
    card.dataset.description = p.description || "";
    card.dataset.startLocal = toPacificLocalInput(p.start);
    card.dataset.endLocal = toPacificLocalInput(p.end);
    return card;
  }

  function renderItem(item) {
    return item.kind === "personal"
      ? renderPersonalItem(item)
      : renderSessionItem(item);
  }

  // ── Top-level view ─────────────────────────────────────────────────────────
  function messageBlock(cls, heading, body) {
    var div = document.createElement("div");
    div.className = "schedule-state " + cls;
    div.innerHTML =
      '<p class="schedule-state-title">' + esc(heading) + "</p>" +
      (body ? '<p class="schedule-state-body">' + esc(body) + "</p>" : "");
    return div;
  }

  // "Favorite details as of 12 min ago" when the backend took favorites'
  // details from its catalog index (fast) rather than re-reading each one live.
  // Reserved sessions are always re-read live, so they are never mentioned here.
  function describeIndexed(indexed) {
    if (!indexed || typeof indexed !== "object" || !indexed.favorites) return "";
    var secs = Math.max(0, Number(indexed.ageSeconds) || 0);
    var mins = Math.floor(secs / 60);
    var age = mins < 1 ? "under a minute"
      : mins < 60 ? mins + " min"
      : Math.floor(mins / 60) + " h" + (mins % 60 ? " " + (mins % 60) + " min" : "");
    return "Favorite details as of " + age + " ago. Reserved sessions are re-checked live.";
  }

  // Add a "Try again" button to an error block when the caller supplied
  // `opts.onRetry` (the schedule dialog does), so a transient failure does not
  // force the user to close and reopen the dialog.
  function addRetry(block, opts) {
    if (!opts || typeof opts.onRetry !== "function") return;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-secondary schedule-retry";
    btn.textContent = "Try again";
    btn.addEventListener("click", function () { opts.onRetry(); });
    block.appendChild(btn);
  }

  // Turn the backend's `incomplete` marker ({kind: {loaded, total}}) into a
  // sentence, or "" when everything loaded.
  function describeIncomplete(incomplete) {
    if (!incomplete || typeof incomplete !== "object") return "";
    var parts = [];
    [["reserved", "reserved sessions"], ["favorites", "favorites"]].forEach(function (k) {
      var inc = incomplete[k[0]];
      if (inc && typeof inc.total === "number" && inc.total > (inc.loaded || 0)) {
        parts.push((inc.loaded || 0) + " of " + inc.total + " " + k[1]);
      }
    });
    return parts.length
      ? "Showing " + parts.join(" and ") + ". The rest couldn't be loaded right now."
      : "";
  }

  // Render the Schedule_View from a backend GET /schedule response.
  //
  //   data   — parsed response body { reserved, favorites, personalTime, message? }
  //   opts   — { error: bool, message: string } to force the retrieve-error state
  //            when the request itself failed (network / non-2xx).
  //
  // Returns a DOM element. Caller inserts it wherever the view lives (10.2).
  function renderScheduleView(data, opts) {
    opts = opts || {};
    var wrap = document.createElement("section");
    wrap.className = "schedule-view";
    wrap.setAttribute("aria-label", "Your schedule");

    // Retrieve-error state: either the fetch failed, or the backend degraded and
    // returned a `message` (timeout / unavailable). No partial or cached view is
    // shown (Req 17.6, 22.4, 23.3).
    var degraded = data && typeof data.message === "string" && data.message;
    if (opts.error || degraded) {
      var errBlock = messageBlock(
        "schedule-error",
        "Your schedule couldn't be retrieved.",
        opts.message || degraded || "Please try again."
      );
      addRetry(errBlock, opts);
      wrap.appendChild(errBlock);
      return wrap;
    }

    data = data || {};
    var reserved = Array.isArray(data.reserved) ? data.reserved : [];
    var favorites = Array.isArray(data.favorites) ? data.favorites : [];
    var personal = Array.isArray(data.personalTime) ? data.personalTime : [];

    // Seed the reserved-id cache from the backend reserved[] array so cards
    // (here and in search results) render with the correct reservation state.
    // Empty until the phased capability is used.
    if (window.Reservations && typeof window.Reservations.setReservedIds === "function") {
      window.Reservations.setReservedIds(reserved.map(function (s) { return s.sessionId; }));
    }

    // Honest completeness: the backend reports, per kind, how many sessions it
    // listed (total) vs. could load (loaded). A cut-off list must never read as
    // the whole schedule, and an unloadable one must never read as "empty".
    var incompleteText = describeIncomplete(data.incomplete);

    // Empty-schedule state (Req 17.5, 23.3): no rows at all.
    if (!reserved.length && !favorites.length && !personal.length) {
      if (incompleteText) {
        var partialBlock = messageBlock(
          "schedule-error",
          "Your schedule couldn't be fully loaded.",
          incompleteText
        );
        addRetry(partialBlock, opts);
        wrap.appendChild(partialBlock);
        return wrap;
      }
      wrap.appendChild(messageBlock(
        "schedule-empty",
        "Your schedule is empty.",
        "Favorite a session or add a personal time block to see it here."
      ));
      return wrap;
    }

    if (incompleteText) {
      var note = document.createElement("p");
      note.className = "schedule-note";
      note.setAttribute("role", "status");
      note.textContent = incompleteText;
      wrap.appendChild(note);
    }

    var indexedText = describeIndexed(data.indexed);
    if (indexedText) {
      var src = document.createElement("p");
      src.className = "schedule-source";
      src.textContent = indexedText;
      wrap.appendChild(src);
    }

    // Tag each item with exactly one kind, order chronologically, and show each
    // Pacific day under its own heading ("Tuesday, Dec 1").
    var list = document.createElement("div");
    list.className = "session-list schedule-list";
    groupByDay(scheduleItems(data)).forEach(function (group) {
      var heading = document.createElement("h3");
      heading.className = "schedule-day-heading";
      heading.textContent = group.key ? fmtDayHeading(group.key) : "Time not set";
      list.appendChild(heading);
      group.items.forEach(function (item) { list.appendChild(renderItem(item)); });
    });
    wrap.appendChild(list);
    return wrap;
  }

  window.Schedule = {
    renderScheduleView: renderScheduleView,
    // Exposed for the form/wiring tasks (10.2–10.4) and unit tests (10.6).
    fmtPacific: fmtPacific,
    parseUtcContract: parseUtcContract,
    toPacificLocalInput: toPacificLocalInput,
    // Used by calendar.js (the .ics export) and unit tests.
    pacificToUtcMs: pacificToUtcMs,
    pacificDateKey: pacificDateKey,
    sessionStartMs: sessionStartMs,
    sessionEndMs: sessionEndMs,
    itemDayKey: itemDayKey,
    fmtDayHeading: fmtDayHeading,
    scheduleItems: scheduleItems,
    groupByDay: groupByDay,
  };
})();
