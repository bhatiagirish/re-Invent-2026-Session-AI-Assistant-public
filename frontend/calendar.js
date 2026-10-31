/* calendar.js — export the schedule as an .ics file ("Add to calendar").
 *
 * Built in the browser from the schedule the dialog has already loaded, so no
 * second request is made and the GitHub Pages demo can export too. The file
 * follows RFC 5545 (iCalendar): CRLF line endings, lines folded at 75 octets,
 * text values escaped, and every time written in UTC ("...Z") so each calendar
 * app shows it in its own time zone.
 *
 * What goes in:
 *   - reserved sessions (CONFIRMED, shown as busy)
 *   - personal time (CONFIRMED, busy)
 *   - favorites only if asked, as TENTATIVE and free (a favorite is a bookmark,
 *     not a held seat); a favorite that is also reserved is exported once
 *   - optionally a reminder alarm (default 15 minutes before)
 * Each event has a stable UID (from the session / personal-time id), so
 * importing again is designed to update events rather than duplicate them.
 * Calendar apps differ in how they treat a re-import.
 */
(function () {
  "use strict";

  // re:Invent 2026 runs Mon Nov 30 - Fri Dec 4 (Pacific).
  var EVENT_DAYS = ["2026-11-30", "2026-12-01", "2026-12-02", "2026-12-03", "2026-12-04"];
  var UID_DOMAIN = "reinvent2026.session-assistant";
  var PRODID = "-//Cloud With Girish//re:Invent 2026 Session Assistant//EN";
  var DEFAULT_REMINDER_MINUTES = 15;

  // ── iCalendar text helpers ──────────────────────────────────────────────────
  function pad(n) { return (n < 10 ? "0" : "") + n; }

  // UTC epoch ms -> "20261202T230000Z".
  function icsUtc(ms) {
    var d = new Date(ms);
    return d.getUTCFullYear() + pad(d.getUTCMonth() + 1) + pad(d.getUTCDate()) +
      "T" + pad(d.getUTCHours()) + pad(d.getUTCMinutes()) + pad(d.getUTCSeconds()) + "Z";
  }

  // Escape a TEXT value (RFC 5545 3.3.11): backslash, newline, semicolon, comma.
  function escapeText(value) {
    return String(value == null ? "" : value)
      .replace(/\\/g, "\\\\")
      .replace(/\r\n|\r|\n/g, "\\n")
      .replace(/;/g, "\\;")
      .replace(/,/g, "\\,");
  }

  function utf8Length(codePoint) {
    if (codePoint < 0x80) return 1;
    if (codePoint < 0x800) return 2;
    if (codePoint < 0x10000) return 3;
    return 4;
  }

  // Fold a content line so no physical line exceeds 75 octets (RFC 5545 3.1).
  // Continuation lines start with one space. A multi-byte character is never
  // split.
  function fold(line) {
    var out = [];
    var current = "";
    var bytes = 0;
    for (var ch of String(line)) {
      var b = utf8Length(ch.codePointAt(0));
      if (bytes + b > 75) {
        out.push(current);
        current = " ";
        bytes = 1;
      }
      current += ch;
      bytes += b;
    }
    out.push(current);
    return out.join("\r\n");
  }

  // ── Events ──────────────────────────────────────────────────────────────────
  function sessionEvent(item) {
    var S = window.Schedule;
    var s = item.data || {};
    var start = S.sessionStartMs(s);
    if (start == null) return null;                    // no time: cannot place it
    var reserved = item.kind === "reserved";
    var description = [reserved ? "Reserved seat" : "Favorite (no seat reserved)"];
    if (s.sessionCode) description.push("Session: " + s.sessionCode);
    if (s.type) description.push("Type: " + s.type);
    if (s.level) description.push("Level: " + s.level);
    if (Array.isArray(s.speakers) && s.speakers.length) {
      description.push("Speakers: " + s.speakers.join(", "));
    }
    description.push("From the AWS re:Invent 2026 Session Assistant. " +
      "Check the AWS Events app for the latest details.");
    var key = s.sessionId || s.sessionCode || String(start);
    return {
      uid: "session-" + key + "@" + UID_DOMAIN,
      summary: [s.sessionCode, s.title].filter(Boolean).join(" - ") || "re:Invent session",
      start: start,
      end: S.sessionEndMs(s),
      location: [s.venue, s.room].filter(Boolean).join(", "),
      description: description.join("\n"),
      status: reserved ? "CONFIRMED" : "TENTATIVE",
      transp: reserved ? "OPAQUE" : "TRANSPARENT",
      category: reserved ? "Reserved session" : "Favorite",
    };
  }

  function personalEvent(p) {
    var S = window.Schedule;
    var start = S.parseUtcContract(p.start);
    if (!start) return null;
    var end = S.parseUtcContract(p.end);
    var key = p.personalTimeId ||
      (start.getTime() + "-" + String(p.title || "").replace(/[^A-Za-z0-9]+/g, "-"));
    return {
      uid: "personal-" + key + "@" + UID_DOMAIN,
      summary: p.title || "Personal time",
      start: start.getTime(),
      end: end ? end.getTime() : null,
      location: p.location || "",
      description: [p.description, "Personal time"].filter(Boolean).join("\n"),
      status: "CONFIRMED",
      transp: "OPAQUE",
      category: "Personal time",
    };
  }

  function eventLines(ev, stampMs, reminderMinutes) {
    var lines = [
      "BEGIN:VEVENT",
      "UID:" + escapeText(ev.uid),
      "DTSTAMP:" + icsUtc(stampMs),
      "DTSTART:" + icsUtc(ev.start),
    ];
    if (ev.end != null && ev.end > ev.start) lines.push("DTEND:" + icsUtc(ev.end));
    lines.push("SUMMARY:" + escapeText(ev.summary));
    if (ev.location) lines.push("LOCATION:" + escapeText(ev.location));
    if (ev.description) lines.push("DESCRIPTION:" + escapeText(ev.description));
    lines.push("STATUS:" + ev.status);
    lines.push("TRANSP:" + ev.transp);
    lines.push("CATEGORIES:" + escapeText(ev.category));
    if (reminderMinutes > 0) {
      lines.push("BEGIN:VALARM", "ACTION:DISPLAY",
        "DESCRIPTION:" + escapeText(ev.summary),
        "TRIGGER:-PT" + reminderMinutes + "M", "END:VALARM");
    }
    lines.push("END:VEVENT");
    return lines;
  }

  // Build the .ics for a loaded schedule.
  //   data  the GET /schedule body the dialog rendered
  //   opts  { day: "YYYY-MM-DD" | null (all days), includeFavorites: bool,
  //           reminderMinutes: number (0 = none), now: epoch ms (for tests) }
  // Returns { text, filename, counts: { reserved, favorites, personal, skipped } }.
  // `skipped` counts items that could not be placed because they have no time.
  function buildIcs(data, opts) {
    opts = opts || {};
    var S = window.Schedule;
    var now = opts.now != null ? opts.now : Date.now();
    var day = opts.day || null;
    var reminder = opts.reminderMinutes > 0 ? Math.floor(opts.reminderMinutes) : 0;
    var items = S.scheduleItems(data);

    var reservedIds = {};
    items.forEach(function (it) {
      if (it.kind === "reserved" && it.data && it.data.sessionId) reservedIds[it.data.sessionId] = true;
    });

    var counts = { reserved: 0, favorites: 0, personal: 0, skipped: 0 };
    var events = [];
    items.forEach(function (it) {
      if (it.kind === "favorite") {
        if (!opts.includeFavorites) return;
        if (it.data && reservedIds[it.data.sessionId]) return;   // exported as reserved
      }
      if (day && S.itemDayKey(it) !== day) return;
      var ev = it.kind === "personal" ? personalEvent(it.data || {}) : sessionEvent(it);
      if (!ev) { counts.skipped++; return; }
      events.push(ev);
      if (it.kind === "reserved") counts.reserved++;
      else if (it.kind === "favorite") counts.favorites++;
      else counts.personal++;
    });

    var calName = "re:Invent 2026" + (day ? " - " + S.fmtDayHeading(day) : "");
    var lines = [
      "BEGIN:VCALENDAR",
      "VERSION:2.0",
      "PRODID:" + PRODID,
      "CALSCALE:GREGORIAN",
      "METHOD:PUBLISH",
      "X-WR-CALNAME:" + escapeText(calName),
    ];
    events.forEach(function (ev) {
      Array.prototype.push.apply(lines, eventLines(ev, now, reminder));
    });
    lines.push("END:VCALENDAR");

    return {
      text: lines.map(fold).join("\r\n") + "\r\n",
      filename: "reinvent-2026-" + (day || "schedule") + ".ics",
      counts: counts,
    };
  }

  // ── Saving the file ──────────────────────────────────────────────────────────
  function saveFile(text, filename) {
    var blob = new Blob([text], { type: "text/calendar;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  // ── Wording ──────────────────────────────────────────────────────────────────
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }

  function describeCounts(c) {
    var parts = [];
    if (c.reserved) parts.push(plural(c.reserved, "reserved session", "reserved sessions"));
    if (c.favorites) parts.push(plural(c.favorites, "favorite", "favorites"));
    if (c.personal) parts.push(plural(c.personal, "personal time block", "personal time blocks"));
    var text = parts.length
      ? "The file will contain " + parts.join(", ").replace(/, ([^,]*)$/, " and $1") + "."
      : "Nothing to add for this selection.";
    if (c.skipped) {
      text += " " + plural(c.skipped, "item has", "items have") +
        " no scheduled time and will be left out.";
    }
    return text;
  }

  // Honest gaps: sessions the backend listed but could not load are not in the
  // loaded schedule, so they cannot be in the file either.
  function describeGaps(data, includeFavorites) {
    var inc = (data && data.incomplete) || {};
    var notes = [];
    function gap(kind, one, many) {
      var g = inc[kind];
      if (g && typeof g.total === "number" && g.total > (g.loaded || 0)) {
        notes.push(plural(g.total - (g.loaded || 0), one, many));
      }
    }
    gap("reserved", "reserved session", "reserved sessions");
    if (includeFavorites) gap("favorites", "favorite", "favorites");
    return notes.length
      ? notes.join(" and ") + " couldn't be loaded and won't be in the file. " +
        "Try again later to include them."
      : "";
  }

  // ── The "Add to calendar" panel ──────────────────────────────────────────────
  function el(tag, className, text) {
    var e = document.createElement(tag);
    if (className) e.className = className;
    if (text != null) e.textContent = text;
    return e;
  }

  function field(id, labelText, control) {
    var wrap = el("div", "pt-field");
    var label = el("label", "pt-label", labelText);
    label.htmlFor = id;
    control.id = id;
    wrap.appendChild(label);
    wrap.appendChild(control);
    return wrap;
  }

  function checkbox(id, labelText, checked, className) {
    var wrap = el("div", "pt-field calendar-check");
    var input = el("input", className);
    input.type = "checkbox";
    input.id = id;
    input.checked = !!checked;
    var label = el("label", "pt-label", labelText);
    label.htmlFor = id;
    wrap.appendChild(input);
    wrap.appendChild(label);
    return { wrap: wrap, input: input };
  }

  // Render the panel into `slot`.
  //   options.data     the loaded schedule
  //   options.onClose  called by Cancel
  //   options.save     (text, filename) -> void; defaults to a browser download
  //   options.now      epoch ms, for tests
  function openPanel(slot, options) {
    options = options || {};
    var data = options.data || {};
    var save = options.save || saveFile;
    var S = window.Schedule;

    slot.innerHTML = "";
    var form = el("div", "pt-form calendar-export");
    form.setAttribute("role", "group");
    form.setAttribute("aria-label", "Add to calendar");
    form.appendChild(el("h3", "pt-form-title", "Add to calendar"));
    form.appendChild(el("p", "calendar-intro",
      "Download an .ics file and open it in Apple Calendar, Google Calendar or " +
      "Outlook. Times are saved in UTC, so they show in your calendar's own time zone."));

    var scope = el("select", "pt-input cal-scope");
    var all = el("option", null, "All days");
    all.value = "";
    scope.appendChild(all);
    EVENT_DAYS.forEach(function (d) {
      var o = el("option", null, S.fmtDayHeading(d));
      o.value = d;
      scope.appendChild(o);
    });
    scope.value = "";
    form.appendChild(field("cal-scope", "Days", scope));

    var fav = checkbox("cal-favorites", "Include favorites (as tentative)", false, "cal-favorites");
    var rem = checkbox("cal-reminder",
      "Add a " + DEFAULT_REMINDER_MINUTES + "-minute reminder", true, "cal-reminder");
    form.appendChild(fav.wrap);
    form.appendChild(rem.wrap);

    var summary = el("p", "calendar-summary cal-summary");
    summary.setAttribute("role", "status");
    summary.setAttribute("aria-live", "polite");
    var warning = el("p", "pt-form-error cal-warning");
    warning.hidden = true;
    form.appendChild(summary);
    form.appendChild(warning);

    var actions = el("div", "pt-actions");
    var download = el("button", "btn btn-primary cal-download", "Download .ics");
    download.type = "button";
    var cancel = el("button", "btn btn-ghost cal-cancel", "Cancel");
    cancel.type = "button";
    actions.appendChild(download);
    actions.appendChild(cancel);
    form.appendChild(actions);
    slot.appendChild(form);

    function current() {
      return buildIcs(data, {
        day: scope.value || null,
        includeFavorites: !!fav.input.checked,
        reminderMinutes: rem.input.checked ? DEFAULT_REMINDER_MINUTES : 0,
        now: options.now,
      });
    }

    function update() {
      var result = current();
      var c = result.counts;
      summary.textContent = describeCounts(c);
      download.disabled = (c.reserved + c.favorites + c.personal) === 0;
      var gaps = describeGaps(data, !!fav.input.checked);
      warning.textContent = gaps;
      warning.hidden = !gaps;
      return result;
    }

    scope.addEventListener("change", update);
    fav.input.addEventListener("change", update);
    rem.input.addEventListener("change", update);
    download.addEventListener("click", function () {
      var result = update();
      var c = result.counts;
      if (!(c.reserved + c.favorites + c.personal)) return;
      save(result.text, result.filename);
      summary.textContent = "Downloaded " + result.filename +
        ". Open it to add the events to your calendar.";
    });
    cancel.addEventListener("click", function () {
      if (typeof options.onClose === "function") options.onClose();
    });

    update();
    return form;
  }

  window.Calendar = {
    buildIcs: buildIcs,
    openPanel: openPanel,
    describeCounts: describeCounts,
    describeGaps: describeGaps,
    EVENT_DAYS: EVENT_DAYS,
    // exposed for unit tests
    _fold: fold,
    _escapeText: escapeText,
    _icsUtc: icsUtc,
  };
})();
