/* demo-api.js — in-browser stand-in for frontend/api.js (the DEMO build).
 *
 * The real UI (chat.js, sessions.js, schedule.js, ...) is reused unchanged. It
 * only talks to `window.API`, so the demo swaps that one object for this mock.
 * Nothing here makes a network request: no AWS, no AWS Events, no Bedrock, no
 * sign-in. State lives in memory and resets when the page is reloaded.
 *
 * Every method resolves to { status, data } with the SAME response shapes the
 * real backend returns (see backend/app.py and backend/models.py), so the real
 * rendering code works untouched.
 *
 * The "assistant" is NOT an AI model. It is a small keyword matcher over the
 * sample data in demo-data.js, just enough to click through the main flows:
 * search, details, favorites, reservations, schedule, personal time, counts.
 */
(function () {
  "use strict";

  var D = window.DEMO_DATA;
  var TZ = "America/Los_Angeles";
  var SEARCH_LIMIT = 5;

  var BY_ID = {};
  var BY_CODE = {};
  D.sessions.forEach(function (s) {
    BY_ID[s.sessionId] = s;
    BY_CODE[s.sessionCode.toUpperCase()] = s;
  });
  var FULL = {};
  (D.fullSessionCodes || []).forEach(function (code) {
    if (BY_CODE[code]) FULL[BY_CODE[code].sessionId] = true;
  });

  var state;

  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function reset() {
    state = {
      favorites: D.initial.favorites.slice(),
      reserved: D.initial.reserved.slice(),
      personalTime: clone(D.initial.personalTime),
      nextPtId: 100,
      lastResults: [],     // session ids shown by the last list ("#N" refs)
      lastSessionId: null, // "that session"
    };
  }
  reset();

  // ── Latency (makes the demo feel like a real round trip) ───────────────────
  function wait(min, max) {
    if (window.DEMO_FAST) return Promise.resolve();
    return new Promise(function (resolve) {
      setTimeout(resolve, min + Math.random() * (max - min));
    });
  }
  function reply(data, status, min, max) {
    return wait(min == null ? 150 : min, max == null ? 380 : max).then(function () {
      return { status: status || 200, data: data };
    });
  }

  // ── Time helpers (real IANA rules via Intl, never a hardcoded offset) ──────
  var ptFormatter = new Intl.DateTimeFormat("en-US", {
    timeZone: TZ, hourCycle: "h23",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
  function ptParts(ms) {
    var out = {};
    ptFormatter.formatToParts(new Date(ms)).forEach(function (p) {
      if (p.type !== "literal") out[p.type] = parseInt(p.value, 10);
    });
    return out;
  }
  // Minutes that Pacific is BEHIND UTC at the given instant (480 in PST).
  function ptOffsetMin(ms) {
    var p = ptParts(ms);
    var asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute);
    var floored = Math.floor(ms / 60000) * 60000;
    return Math.round((floored - asUtc) / 60000);
  }
  function pacificToUtcMs(y, mo, d, h, mi) {
    var guess = Date.UTC(y, mo - 1, d, h, mi);
    var utc = guess + ptOffsetMin(guess) * 60000;
    return guess + ptOffsetMin(utc) * 60000;
  }
  function sessionStartMs(s) {
    var d = s.sessionTime.date.split("-");
    var t = s.sessionTime.time.split(":");
    return pacificToUtcMs(+d[0], +d[1], +d[2], +t[0], +t[1]);
  }
  function sessionEndMs(s) { return sessionStartMs(s) + s.sessionTime.length * 60000; }
  function utcContract(ms) { return new Date(ms).toISOString().slice(0, 19); }
  function parseUtcContract(str) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})$/.exec(str || "");
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : NaN;
  }
  function parsePacificLocal(str) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(str || "");
    return m ? pacificToUtcMs(+m[1], +m[2], +m[3], +m[4], +m[5]) : null;
  }
  function ptDateOf(ms) {
    var p = ptParts(ms);
    return p.year + "-" + pad(p.month) + "-" + pad(p.day);
  }

  var fmtShort = new Intl.DateTimeFormat("en-US", {
    timeZone: TZ, weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
  var fmtTime = new Intl.DateTimeFormat("en-US", {
    timeZone: TZ, hour: "numeric", minute: "2-digit",
  });
  var fmtDay = new Intl.DateTimeFormat("en-US", {
    timeZone: TZ, weekday: "short", month: "short", day: "numeric",
  });
  function fmtRange(a, b) {
    return fmtShort.format(new Date(a)) + " to " + fmtTime.format(new Date(b)) + " PT";
  }
  function fmtDate(isoDate) {
    var d = isoDate.split("-");
    return fmtDay.format(new Date(pacificToUtcMs(+d[0], +d[1], +d[2], 12, 0)));
  }

  // ── Session helpers ────────────────────────────────────────────────────────
  function pub(s) { return clone(s); }
  function bySessionStart(a, b) { return sessionStartMs(a) - sessionStartMs(b); }
  function idsToSessions(ids) {
    return ids.map(function (id) { return BY_ID[id]; }).filter(Boolean).sort(bySessionStart);
  }
  function sortedPersonalTime() {
    return state.personalTime.slice().sort(function (a, b) {
      var byStart = parseUtcContract(a.start) - parseUtcContract(b.start);
      if (byStart) return byStart;
      return String(a.title).toLowerCase() < String(b.title).toLowerCase() ? -1 : 1;
    });
  }
  function sessionLine(s) {
    return "**" + s.sessionCode + "** \u2014 " + s.title + " (" +
      fmtShort.format(new Date(sessionStartMs(s))) + ")";
  }
  function setLast(sessions) {
    state.lastResults = sessions.map(function (s) { return s.sessionId; });
    if (sessions.length === 1) state.lastSessionId = sessions[0].sessionId;
  }

  // ── Schedule ───────────────────────────────────────────────────────────────
  function buildSchedule(dateFilter) {
    var reserved = idsToSessions(state.reserved);
    var favorites = idsToSessions(state.favorites);
    var pt = sortedPersonalTime();
    if (dateFilter) {
      reserved = reserved.filter(function (s) { return s.sessionTime.date === dateFilter; });
      favorites = favorites.filter(function (s) { return s.sessionTime.date === dateFilter; });
      pt = pt.filter(function (p) { return ptDateOf(parseUtcContract(p.start)) === dateFilter; });
    }
    return {
      reserved: reserved.map(pub),
      favorites: favorites.map(pub),
      personalTime: clone(pt),
    };
  }

  // ── Favorites / reservations (same truthfulness rules as the backend) ──────
  function favAction(type, status, s, reason) {
    var a = { type: type, status: status, sessionId: s.sessionId, sessionCode: s.sessionCode };
    if (reason) a.reason = reason;
    return a;
  }
  function doFavorite(id) {
    var s = BY_ID[id];
    if (!s) return { type: "favorite", status: "failed", sessionId: id, reason: "could_not_favorite" };
    if (state.favorites.indexOf(id) !== -1) return favAction("favorite", "success", s, "already_favorited");
    state.favorites.push(id);
    return favAction("favorite", "success", s);
  }
  function doUnfavorite(id) {
    var s = BY_ID[id];
    if (!s) return { type: "unfavorite", status: "failed", sessionId: id, reason: "could_not_unfavorite" };
    var i = state.favorites.indexOf(id);
    if (i === -1) return favAction("unfavorite", "success", s, "not_currently_favorited");
    state.favorites.splice(i, 1);
    return favAction("unfavorite", "success", s);
  }
  function doReserve(id) {
    var s = BY_ID[id];
    if (!s) return { type: "reserve", status: "failed", sessionId: id, reason: "could_not_reserve" };
    if (state.reserved.indexOf(id) !== -1) return favAction("reserve", "success", s, "already_reserved");
    if (!s.isReservable) return favAction("reserve", "failed", s, "could_not_reserve");
    if (FULL[id]) return favAction("reserve", "failed", s, "session_full");
    var start = sessionStartMs(s), end = sessionEndMs(s);
    var clash = state.reserved.some(function (rid) {
      var o = BY_ID[rid];
      return o && start < sessionEndMs(o) && end > sessionStartMs(o);
    });
    if (clash) return favAction("reserve", "failed", s, "schedule_conflict");
    state.reserved.push(id);
    return favAction("reserve", "success", s);
  }
  function doCancel(id) {
    var s = BY_ID[id];
    if (!s) return { type: "cancel-reservation", status: "failed", sessionId: id, reason: "could_not_cancel_reservation" };
    var i = state.reserved.indexOf(id);
    if (i === -1) return favAction("cancel-reservation", "success", s, "not_currently_reserved");
    state.reserved.splice(i, 1);
    return favAction("cancel-reservation", "success", s);
  }
  // The chat box changes reservation state without going through the card
  // buttons, so nudge the real Reservations module to repaint its buttons.
  function syncReservationUi(id, reserved) {
    try {
      if (window.Reservations && typeof window.Reservations.markReserved === "function") {
        window.Reservations.markReserved(id, reserved);
        if (typeof window.Reservations.refreshAllCards === "function") {
          window.Reservations.refreshAllCards();
        }
      }
    } catch (_) { /* cosmetic only */ }
  }

  // ── Personal time ──────────────────────────────────────────────────────────
  var TITLE_MAX = 200, DESC_MAX = 1000, LOC_MAX = 200;
  function validatePt(e) {
    e = e || {};
    var title = typeof e.title === "string" ? e.title : "";
    if (!title.trim()) return "A title is required.";
    if (title.length > TITLE_MAX) return "The title must be " + TITLE_MAX + " characters or fewer.";
    if ((e.description || "").length > DESC_MAX) return "The description must be " + DESC_MAX + " characters or fewer.";
    if ((e.location || "").length > LOC_MAX) return "The location must be " + LOC_MAX + " characters or fewer.";
    var a = parsePacificLocal(e.start), b = parsePacificLocal(e.end);
    if (a == null) return "The start time must look like YYYY-MM-DDTHH:MM (Pacific event-local time).";
    if (b == null) return "The end time must look like YYYY-MM-DDTHH:MM (Pacific event-local time).";
    if (b <= a) return "The end time must be after the start time.";
    if (((b - a) / 60000) % 5 !== 0) return "The duration must be a whole number of 5-minute increments.";
    return null;
  }
  function buildPt(id, e) {
    var out = {
      personalTimeId: id,
      title: e.title.trim(),
      start: utcContract(parsePacificLocal(e.start)),
      end: utcContract(parsePacificLocal(e.end)),
    };
    if (e.description) out.description = e.description;
    if (e.location) out.location = e.location;
    return out;
  }
  function retained(e, id) {
    var r = {
      title: e.title, start: e.start, end: e.end,
      description: e.description || null, location: e.location || null,
    };
    if (id) r.personalTimeId = id;
    return r;
  }
  function addPt(e) {
    var id = "demo-pt-" + (state.nextPtId++);
    var entry = buildPt(id, e);
    state.personalTime.push(entry);
    return entry;
  }
  function invalid(message) {
    return { error: { category: "VALIDATION_ERROR", message: message } };
  }

  // ── The demo "assistant" (keyword matcher, not an AI model) ────────────────
  var WEEKDAYS = {
    monday: "2026-11-30", tuesday: "2026-12-01", wednesday: "2026-12-02",
    thursday: "2026-12-03", friday: "2026-12-04",
  };
  var MONTHS = { nov: 11, november: 11, dec: 12, december: 12 };
  function parseDate(lower) {
    var m = lower.match(/\b(2026-\d{2}-\d{2})\b/);
    if (m) return m[1];
    m = lower.match(/\b(nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b/);
    if (m) return "2026-" + pad(MONTHS[m[1]]) + "-" + pad(+m[2]);
    m = lower.match(/\b(monday|tuesday|wednesday|thursday|friday)\b/);
    return m ? WEEKDAYS[m[1]] : null;
  }

  var ORDINALS = { first: 0, second: 1, third: 2, fourth: 3, fifth: 4 };
  function resolveRef(msg, lower) {
    var m = msg.match(/\b([A-Za-z]{3}\d{3})\b/);
    if (m && BY_CODE[m[1].toUpperCase()]) return BY_CODE[m[1].toUpperCase()];
    m = lower.match(/#\s*(\d+)/);
    if (m) {
      var id = state.lastResults[+m[1] - 1];
      return id ? BY_ID[id] : null;
    }
    m = lower.match(/\b(first|second|third|fourth|fifth)\b/);
    if (m && state.lastResults[ORDINALS[m[1]]]) return BY_ID[state.lastResults[ORDINALS[m[1]]]];
    if (/\b(that|this|it)\b/.test(lower) && state.lastSessionId) return BY_ID[state.lastSessionId];
    return null;
  }

  var STOP = {};
  ("a an the and or of to for on in at with about me my i we you your is are was be do does did " +
   "can could would should please show find list give get any some there what which who how many " +
   "much want need like looking sessions session tell more details detail info information that this " +
   "it its those these from by into over total number count have has all amazon aws").split(" ")
    .forEach(function (w) { STOP[w] = true; });
  var NOISE = {};
  ("beginner beginners friendly beginner-friendly intro introductory introduction foundational advanced " +
   "expert level levels nov november dec december monday tuesday wednesday thursday friday " +
   "workshop workshops chalk talk talks builders builder code keynote keynotes breakout leadership " +
   "venue topic type favorite favorites favorited reserved reservation reservations grouped group " +
   "catalog sample demo").split(" ").forEach(function (w) { NOISE[w] = true; });
  var SYNONYMS = { genai: "generative", llm: "generative", ml: "machine", agents: "agent" };

  var TYPES = [
    { re: /\bworkshops?\b/, type: "Workshop" },
    { re: /\bchalk\s*talks?\b/, type: "Chalk talk" },
    { re: /\bbuilders?'?\s*sessions?\b|\bbuilders?\b/, type: "Builders' session" },
    { re: /\bcode\s*talks?\b/, type: "Code talk" },
    { re: /\bkeynotes?\b/, type: "Keynote" },
    { re: /\bbreakouts?\b/, type: "Breakout session" },
    { re: /\bleadership\b/, type: "Leadership session" },
  ];
  var VENUES = [
    { re: /\bvenetian\b/, name: "Venetian" },
    { re: /\bwynn\b|\bencore\b/, name: "Wynn/Encore" },
    { re: /\bmgm\b/, name: "MGM Grand" },
    { re: /\bcaesars\b/, name: "Caesars Forum" },
    { re: /\bmandalay\b/, name: "Mandalay Bay" },
  ];

  function levelFilter(lower) {
    if (/\bbeginner|\bintro|\bfoundational|\bgetting started|\bnew to\b|\bbasics?\b/.test(lower)) return ["100", "200"];
    if (/\bexpert\b/.test(lower)) return ["400"];
    if (/\badvanced\b/.test(lower)) return ["300", "400"];
    var m = lower.match(/\blevel\s*(100|200|300|400)\b/);
    return m ? [m[1]] : null;
  }

  function parseFilters(lower) {
    var f = { tokens: [], type: null, levels: levelFilter(lower), date: parseDate(lower), venue: null };
    TYPES.forEach(function (t) { if (!f.type && t.re.test(lower)) f.type = t.type; });
    VENUES.forEach(function (v) { if (!f.venue && v.re.test(lower)) f.venue = v.name; });
    var cleaned = lower
      .replace(/\b2026-\d{2}-\d{2}\b/g, " ")
      .replace(/[^a-z0-9\s'-]/g, " ");
    cleaned.split(/\s+/).forEach(function (w) {
      if (!w || w.length < 2) return;
      if (STOP[w] || NOISE[w] || /^\d+(st|nd|rd|th)?$/.test(w)) return;
      if (VENUES.some(function (v) { return v.re.test(w); })) return;
      if (/^(workshops?|builders?|keynotes?|breakouts?)$/.test(w)) return;
      f.tokens.push(SYNONYMS[w] || w);
    });
    return f;
  }

  function hayOf(s) {
    return [s.sessionCode, s.title, s.type, s.level, s.venue, s.services.join(" "),
            s.topics.join(" "), s.abstract || ""].join(" ").toLowerCase();
  }
  function tokenMatches(hay, token) {
    if (token.length <= 3) return new RegExp("\\b" + token + "\\b").test(hay);
    return hay.indexOf(token) !== -1;
  }
  // Returns matching sessions with scores, best first. With no keyword tokens
  // the structured filters alone decide.
  function filterSessions(list, f) {
    var scored = [];
    list.forEach(function (s) {
      if (f.type && s.type !== f.type) return;
      if (f.levels && !f.levels.some(function (l) { return s.level.indexOf(l) === 0; })) return;
      if (f.date && s.sessionTime.date !== f.date) return;
      if (f.venue && s.venue !== f.venue) return;
      var score = 0;
      if (f.tokens.length) {
        var hay = hayOf(s);
        f.tokens.forEach(function (t) { if (tokenMatches(hay, t)) score++; });
        if (!score) return;
      }
      scored.push({ s: s, score: score });
    });
    // Prefer sessions that match most of the keywords; fall back to any match.
    if (f.tokens.length) {
      var need = Math.max(1, Math.ceil(f.tokens.length * 0.6));
      var strong = scored.filter(function (x) { return x.score >= need; });
      if (strong.length) scored = strong;
    }
    scored.sort(function (a, b) {
      return b.score - a.score || bySessionStart(a.s, b.s);
    });
    return scored.map(function (x) { return x.s; });
  }
  function describeFilters(f) {
    var parts = [];
    if (f.tokens.length) parts.push("matching \u201c" + f.tokens.join(" ") + "\u201d");
    if (f.type) parts.push("of type " + f.type);
    if (f.levels) parts.push("at level " + f.levels.join("/"));
    if (f.venue) parts.push("at " + f.venue);
    if (f.date) parts.push("on " + fmtDate(f.date));
    return parts.join(", ");
  }

  // -- time-range parsing for "add lunch Dec 1 noon to 1pm" -------------------
  function to24(h, ap) {
    if (ap === "pm" && h < 12) return h + 12;
    if (ap === "am" && h === 12) return 0;
    return h;
  }
  function guess24(h) {              // no am/pm: conference-day heuristic
    if (h >= 13) return h;
    if (h === 12) return 12;
    return h <= 6 ? h + 12 : h;      // 1-6 -> afternoon, 7-11 -> morning
  }
  function parseTimeRange(text) {
    var t = text.replace(/\b\d{4}-\d{2}-\d{2}\b/g, " ")
      .replace(/\bnoon\b/g, "12:00 pm").replace(/\bmidnight\b/g, "12:00 am");
    var m = t.match(/(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:to|-|\u2013|until|till)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?/);
    if (!m) return null;
    var sh = +m[1], sm = +(m[2] || 0), sa = m[3];
    var eh = +m[4], em = +(m[5] || 0), ea = m[6];
    var s24, e24;
    if (sa || ea) {
      if (!sa) sa = ea;
      if (!ea) ea = sa;
      s24 = to24(sh, sa);
      e24 = to24(eh, ea);
      if (e24 * 60 + em <= s24 * 60 + sm) {
        if (!m[3] && sa === "pm") s24 = to24(sh, "am");     // "11 to 1 pm"
        else if (!m[6] && ea === "am") e24 = to24(eh, "pm"); // "11 am to 1"
      }
    } else {
      s24 = guess24(sh);
      e24 = guess24(eh);
      if (e24 * 60 + em <= s24 * 60 + sm && e24 < 12) e24 += 12;
    }
    if (s24 > 23 || e24 > 23 || sm > 59 || em > 59) return null;
    if (e24 * 60 + em <= s24 * 60 + sm) return null;
    return { sh: s24, sm: sm, eh: e24, em: em };
  }
  var PT_TITLES = [
    [/\blunch\b/, "Lunch"], [/\bdinner\b/, "Dinner"], [/\bbreakfast\b/, "Breakfast"],
    [/\bcoffee\b/, "Coffee chat"], [/\bmeeting\b/, "Meeting"], [/\bcall\b/, "Call"],
    [/\bnetworking\b/, "Networking"], [/\bexpo\b/, "Expo time"], [/\bbreak\b/, "Break"],
  ];
  function localString(date, h, m) { return date + "T" + pad(h) + ":" + pad(m); }

  // Each handler returns a /chat response body.
  function help(convId) {
    return {
      conversationId: convId,
      message: "I'm the **demo** assistant. I match keywords over a small set of sample sessions " +
        "(no AI model is running here). Try one of these:\n\n" +
        "- Find sessions about Amazon Bedrock\n" +
        "- Tell me more about AIM301\n" +
        "- Favorite #2, or Reserve a seat for DVT201\n" +
        "- Show my schedule for 2026-12-02\n" +
        "- Add lunch Dec 1 noon to 1pm\n" +
        "- How many sessions are on Dec 3?",
      sessions: [], action: null,
    };
  }

  function handleChat(convId, message) {
    var msg = String(message || "");
    var lower = msg.toLowerCase();
    var base = { conversationId: convId, sessions: [], action: null };
    function say(text, extra) { return Object.assign({}, base, { message: text }, extra || {}); }

    if (/^\s*(hi|hello|hey|help)\b|what can you do/.test(lower)) return help(convId);

    var hasPtWord = /personal time|\bblock\b|lunch|dinner|breakfast|coffee|meeting|\bcall\b|keynote|networking|\bexpo\b|\bbreak\b|badge/.test(lower);
    var mentionsFav = /favorit|bookmark|\bstar\b/.test(lower);
    var mentionsResv = /reserv|\bseat\b/.test(lower);

    // ---- schedule view ------------------------------------------------------
    var wantsSchedule = /\b(schedule|agenda|calendar|itinerary)\b/.test(lower) ||
      (/personal time/.test(lower) && /\b(show|list|view|what)\b/.test(lower));
    if (wantsSchedule &&
        !/\b(add|create|put|block|delete|remove|change|move|update|edit|reschedule)\b/.test(lower)) {
      var day = parseDate(lower);
      var sched = buildSchedule(day);
      var n = sched.reserved.length + sched.favorites.length + sched.personalTime.length;
      var where = day ? "for **" + fmtDate(day) + "**" : "";
      var text = n
        ? "Here's your schedule " + where + " (sample data): " + sched.reserved.length +
          " reserved, " + sched.favorites.length + " favorite" + (sched.favorites.length === 1 ? "" : "s") +
          ", and " + sched.personalTime.length + " personal time block" +
          (sched.personalTime.length === 1 ? "" : "s") + ". Times are Pacific."
        : "Nothing is on your schedule " + (day ? "for **" + fmtDate(day) + "** " : "") + "yet.";
      return say(text, { schedule: sched });
    }

    // ---- personal time: add / delete / edit -----------------------------------
    if (hasPtWord && !mentionsFav && !mentionsResv) {
      if (/\b(delete|remove|clear)\b/.test(lower)) {
        var list = sortedPersonalTime();
        var target = null;
        var hm = lower.match(/#\s*(\d+)/);
        if (hm) target = list[+hm[1] - 1] || null;
        if (!target) {
          list.forEach(function (p) {
            var first = String(p.title).toLowerCase().split(/\s+/)[0];
            if (!target && first && lower.indexOf(first) !== -1) target = p;
          });
        }
        if (!target) return say("Which personal time block should I delete? Say something like \u201cDelete my #1 personal time\u201d, or open **My Schedule**.");
        state.personalTime = state.personalTime.filter(function (p) { return p.personalTimeId !== target.personalTimeId; });
        return say("Deleted **" + target.title + "** from your schedule.");
      }
      if (/\b(change|move|update|edit|reschedule|shift)\b/.test(lower)) {
        return say("In this demo you can edit a block from **My Schedule**: open it and use **Edit** on the personal time card.");
      }
      if (/\b(add|create|put|block|book|schedule)\b/.test(lower)) {
        var date = parseDate(lower) || "2026-12-01";
        var range = parseTimeRange(lower);
        var title = "Personal time";
        var quoted = msg.match(/["\u201c]([^"\u201d]{1,80})["\u201d]/);
        if (quoted) title = quoted[1];
        else PT_TITLES.some(function (pt) {
          if (pt[0].test(lower)) { title = pt[1]; return true; }
          return false;
        });
        if (!range && /keynote/.test(lower)) {
          range = { sh: 8, sm: 0, eh: 10, em: 0 };
          if (!quoted) title = "Keynote (sample)";
          if (!/(2026-\d{2}-\d{2}|nov|dec)/.test(lower) && !parseDate(lower)) date = "2026-12-01";
        }
        if (!range) {
          return say("I need a start and end time. Try \u201cAdd lunch Dec 1 from 12:00 to 1:00 PM\u201d. Times are Pacific.");
        }
        var entryIn = {
          title: title,
          start: localString(date, range.sh, range.sm),
          end: localString(date, range.eh, range.em),
        };
        var err = validatePt(entryIn);
        if (err) return say("I couldn't add that: " + err);
        var entry = addPt(entryIn);
        return say("Added **" + entry.title + "** on " +
          fmtRange(parseUtcContract(entry.start), parseUtcContract(entry.end)) +
          ". Open **My Schedule** to see it.");
      }
    }

    // ---- counts -------------------------------------------------------------
    if (/how many|\bcount\b|number of|\bby (venue|date|day|topic|type|level)\b|\bgrouped\b/.test(lower)) {
      var scope = /favorit/.test(lower) ? "favorites" : (/reserv/.test(lower) ? "reserved" : "catalog");
      var pool = scope === "favorites" ? idsToSessions(state.favorites)
        : scope === "reserved" ? idsToSessions(state.reserved) : D.sessions.slice().sort(bySessionStart);
      var filters = parseFilters(lower);
      var hits = filterSessions(pool, filters);
      var scopeText = scope === "favorites" ? "in your favorites"
        : scope === "reserved" ? "in your reservations" : "in the sample catalog";
      var desc = describeFilters(filters);
      var gm = lower.match(/\bby (venue|date|day|topic|type|level)\b/);
      var out = "I found **" + hits.length + "** session" + (hits.length === 1 ? "" : "s") + " " +
        scopeText + (desc ? " " + desc : "") + ".";
      if (gm && hits.length) {
        var key = gm[1] === "day" ? "date" : gm[1];
        var counts = {};
        hits.forEach(function (s) {
          var keys = key === "venue" ? [s.venue]
            : key === "date" ? [fmtDate(s.sessionTime.date)]
            : key === "type" ? [s.type]
            : key === "level" ? [s.level]
            : (s.topics.length ? s.topics : ["(none)"]);
          keys.forEach(function (k) { counts[k] = (counts[k] || 0) + 1; });
        });
        out += "\n\nBy " + key + ":\n\n" + Object.keys(counts)
          .sort(function (a, b) { return counts[b] - counts[a] || (a < b ? -1 : 1); })
          .map(function (k) { return "- " + k + ": " + counts[k]; }).join("\n");
      }
      return say(out);
    }

    // ---- favorites ----------------------------------------------------------
    if (mentionsFav) {
      var fref = resolveRef(msg, lower);
      if (/\b(unfavorite|unstar|remove)\b/.test(lower) || /un-?favorite/.test(lower)) {
        if (!fref) return say("Which session should I remove from your favorites? Try \u201cUnfavorite AIM301\u201d.");
        var ua = doUnfavorite(fref.sessionId);
        state.lastSessionId = fref.sessionId;
        return say(ua.reason === "not_currently_favorited"
          ? "**" + fref.sessionCode + "** wasn't in your favorites."
          : "Removed **" + fref.sessionCode + "** from your favorites.", { action: ua });
      }
      if (fref && !/\b(show|list|view)\b/.test(lower)) {
        var fa = doFavorite(fref.sessionId);
        state.lastSessionId = fref.sessionId;
        return say(fa.reason === "already_favorited"
          ? "**" + fref.sessionCode + "** is already in your favorites."
          : "Done. **" + fref.sessionCode + "** is now in your favorites.", { action: fa });
      }
      if (/\b(show|list|view|what|my)\b/.test(lower)) {
        var favs = idsToSessions(state.favorites);
        setLast(favs);
        return say(favs.length ? "You have " + favs.length + " favorite" + (favs.length === 1 ? "" : "s") + "."
          : "You don't have any favorites yet.", { sessions: favs.map(pub) });
      }
      return say("Which session should I favorite? Try \u201cFavorite #2\u201d or \u201cFavorite AIM301\u201d.");
    }

    // ---- reservations -------------------------------------------------------
    if (mentionsResv) {
      var rref = resolveRef(msg, lower);
      if (/\b(cancel|unreserve|release|drop)\b/.test(lower)) {
        if (!rref) return say("Which reservation should I cancel? Try \u201cCancel my reservation for AIM340\u201d.");
        var ca = doCancel(rref.sessionId);
        state.lastSessionId = rref.sessionId;
        if (ca.status === "success") syncReservationUi(rref.sessionId, false);
        return say(ca.reason === "not_currently_reserved"
          ? "You didn't have a seat reserved for **" + rref.sessionCode + "**."
          : "Cancelled your reservation for **" + rref.sessionCode + "**.");
      }
      if (rref && !/\b(show|list|view)\b/.test(lower)) {
        var ra = doReserve(rref.sessionId);
        state.lastSessionId = rref.sessionId;
        if (ra.status === "success") {
          syncReservationUi(rref.sessionId, true);
          return say(ra.reason === "already_reserved"
            ? "You already have a seat for **" + rref.sessionCode + "**."
            : "Reserved a seat for **" + rref.sessionCode + "**. (Sample data, no real seat is held.)");
        }
        var why = {
          session_full: "That session is full, so no seat was reserved. Walk-up seating may be available at the room.",
          schedule_conflict: "That session clashes with something already on your schedule, so no seat was reserved.",
          could_not_reserve: "That session is walk-up only, so it can't be reserved.",
        };
        return say(why[ra.reason] || "I couldn't reserve that session.");
      }
      var resvd = idsToSessions(state.reserved);
      setLast(resvd);
      return say(resvd.length ? "You have " + resvd.length + " reserved session" + (resvd.length === 1 ? "" : "s") + "."
        : "You don't have any reserved sessions yet.", { sessions: resvd.map(pub) });
    }

    // ---- details ------------------------------------------------------------
    if (/tell me more|more about|\bdetails?\b|describe|abstract|\bspeakers?\b|who is|who are/.test(lower)) {
      var dref = resolveRef(msg, lower);
      if (dref) {
        state.lastSessionId = dref.sessionId;
        state.lastResults = [dref.sessionId];
        return say("**" + dref.sessionCode + "** \u2014 " + dref.title + "\n\n" +
          "- When: " + fmtRange(sessionStartMs(dref), sessionEndMs(dref)) + "\n" +
          "- Where: " + dref.venue + ", " + dref.room + "\n" +
          "- Speakers: " + (dref.speakers.join(", ") || "none listed") + "\n\n" + dref.abstract,
          { sessions: [pub(dref)] });
      }
      if (/tell me more|more about|\bdetails?\b|describe|abstract/.test(lower)) {
        return say("Which session? Try \u201cTell me more about AIM301\u201d or \u201cTell me more about #2\u201d.");
      }
    }

    // ---- search -------------------------------------------------------------
    var f = parseFilters(lower);
    var hasFilter = f.tokens.length || f.type || f.levels || f.date || f.venue;
    if (hasFilter) {
      var all = filterSessions(D.sessions.slice(), f);
      if (!all.length) {
        return say("I didn't find a sample session " + (describeFilters(f) || "for that") +
          ". The demo only has about twenty sessions. Try \u201cBedrock\u201d, \u201cserverless\u201d, \u201cKiro\u201d, or \u201cworkshops\u201d.");
      }
      var shown = all.slice(0, SEARCH_LIMIT);
      setLast(shown);
      var lines = shown.map(function (s, i) { return (i + 1) + ". " + sessionLine(s); }).join("\n");
      return say("Here " + (shown.length === 1 ? "is 1 sample session" : "are " + shown.length + " sample sessions") +
        (all.length > shown.length ? " (top " + shown.length + " of " + all.length + ")" : "") +
        (describeFilters(f) ? " " + describeFilters(f) : "") + ":\n\n" + lines +
        "\n\nTry \u201cTell me more about #1\u201d, \u201cFavorite #2\u201d, or \u201cReserve #3\u201d.",
        { sessions: shown.map(pub) });
    }

    return help(convId);
  }

  // ── The mock window.API ─────────────────────────────────────────────────────
  var csrf = null;
  window.API = {
    setCsrfToken: function (t) { csrf = t || null; },
    authStatus: function () {
      return reply({
        authenticated: true, eventAccess: "authorized",
        csrfToken: "demo-csrf", reservationsEnabled: true,
      }, 200, 60, 160);
    },
    loginUrl: function () { return "#"; },
    logout: function () { return reply({ loggedOut: true }); },
    claim: function () { return reply({ connected: true, eventAccess: "authorized" }); },

    chat: function (conversationId, message) {
      return wait(550, 1100).then(function () {
        return { status: 200, data: handleChat(conversationId, message) };
      });
    },

    getFavorites: function () {
      return reply({ sessions: idsToSessions(state.favorites).map(pub) });
    },
    favoriteIds: function () { return reply({ favoriteIds: state.favorites.slice() }, 200, 40, 120); },
    favorite: function (id) { return reply({ action: doFavorite(id) }); },
    unfavorite: function (id) { return reply({ action: doUnfavorite(id) }); },

    getSchedule: function () { return reply(buildSchedule(null), 200, 200, 450); },

    createPersonalTime: function (entry) {
      var err = validatePt(entry);
      if (err) return reply(invalid(err), 400);
      var created = addPt(entry);
      return reply({ action: { type: "create", status: "success",
        personalTime: clone(created), personalTimeId: created.personalTimeId } });
    },
    updatePersonalTime: function (id, entry) {
      var idx = -1;
      state.personalTime.forEach(function (p, i) { if (p.personalTimeId === id) idx = i; });
      if (idx === -1) {
        return reply({ action: { type: "update", status: "not_found", reason: "not_found",
          personalTimeId: id, message: "That personal time block no longer exists.",
          retainedInput: retained(entry || {}, id) } });
      }
      var err = validatePt(entry);
      if (err) return reply(invalid(err), 400);
      var updated = buildPt(id, entry);     // full replacement: cleared fields stay cleared
      state.personalTime[idx] = updated;
      return reply({ action: { type: "update", status: "success",
        personalTime: clone(updated), personalTimeId: id } });
    },
    deletePersonalTime: function (id) {
      state.personalTime = state.personalTime.filter(function (p) { return p.personalTimeId !== id; });
      return reply({ action: { type: "delete", status: "success", personalTimeId: id } });
    },

    // The demo searches its own small sample set directly, so there is no
    // catalog index to build; report "ready" so the page stops asking.
    warmCatalog: function () {
      return reply({ enabled: true, ready: true, building: false,
                     sessions: D.sessions.length, totalCount: D.sessions.length }, 200, 20, 60);
    },
    getReservations: function () {
      return reply({ sessions: idsToSessions(state.reserved).map(pub) });
    },
    reservedIds: function () { return reply({ reservedIds: state.reserved.slice() }, 200, 40, 120); },
    reserve: function (id) {
      var a = doReserve(id);
      return reply({ action: a });
    },
    cancelReservation: function (id) {
      return reply({ action: doCancel(id) });
    },
  };

  // Exposed for the demo page ("Reset demo") and for tests.
  window.DemoApi = { reset: reset, handleChat: handleChat, _state: function () { return state; } };
})();
