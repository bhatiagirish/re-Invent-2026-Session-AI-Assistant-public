/* catalog.js — keep the server-side catalog index warm, and let other parts of
 * the page wait for it.
 *
 * Searching the whole catalog, and loading favorites' details quickly, needs an
 * index the backend builds in bounded steps (POST /catalog/warm). After the page
 * authorizes, start() asks for a build in the background and keeps asking until
 * the backend says it is ready, then re-checks occasionally (the backend only
 * does real work when the index is missing, old, or the catalog size changed).
 *
 * whenReady() lets a caller wait for that build. The schedule dialog uses it:
 * while the index is still being built (about a minute after a deploy, or after
 * a long absence), loading the schedule would take the slow per-session path
 * that tends to fail, so the dialog waits for the index instead.
 *
 * Failures are silent and non-blocking: this is an optimization, never a gate.
 */
(function () {
  var MAX_ROUNDS = 12;            // ~12 bounded steps is far more than a build needs
  var ROUND_DELAY_MS = 1500;      // pause between steps while building
  var RECHECK_MS = 10 * 60 * 1000;
  // A single failed step (an AWS Events hiccup) must not abandon the build: the
  // backend saved its progress, so the next step resumes. Give up only after
  // this many failures IN A ROW, pausing between them.
  var MAX_CONSECUTIVE_ERRORS = 3;
  var ERROR_DELAY_MS = 3000;

  var running = false;

  // Last known state of the server-side index:
  //   "idle"        nothing asked yet
  //   "checking"    the first question is in flight; the backend has not said
  //                 yet whether the index is usable (usually it is, and the
  //                 answer takes a second or two)
  //   "warming"     the backend CONFIRMED a build is in progress (not usable yet)
  //   "ready"       the index is usable
  //   "failed"      the last attempt errored or gave up (nothing to wait for)
  //   "unavailable" the feature is switched off, or the API lacks it
  var status = "idle";
  var readyWaiters = [];     // whenReady(): settled when the index is usable or not
  var knownWaiters = [];     // whenKnown(): settled when the first answer arrives

  function flush(list, value) {
    list.forEach(function (fn) { fn(value); });
  }

  // Change state and settle whoever is waiting on it.
  function setStatus(next) {
    status = next;
    if (next !== "checking") {
      var known = knownWaiters;
      knownWaiters = [];
      flush(known, true);
    }
    if (next !== "checking" && next !== "warming") {
      var ready = readyWaiters;
      readyWaiters = [];
      flush(ready, next === "ready");
    }
  }

  function defaultSleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  // Ask for build steps until ready. Returns why it stopped:
  //   "ready" | "disabled" | "error" | "busy" | "gave_up".
  async function warm(opts) {
    opts = opts || {};
    var sleep = opts.sleep || defaultSleep;
    if (running) return "busy";               // never overlap two warm loops
    if (!window.API || typeof window.API.warmCatalog !== "function") {
      setStatus("unavailable");
      return "disabled";
    }
    running = true;
    // A periodic re-check of an index that is already usable must not make
    // anyone wait: the backend keeps serving it while it refreshes.
    var lastReady = status === "ready";
    if (!lastReady) setStatus("checking");
    try {
      var errors = 0;
      for (var round = 0; round < MAX_ROUNDS; round++) {
        var res;
        try {
          res = await window.API.warmCatalog();
        } catch (err) {
          // An expired session cannot succeed until the user reconnects, so stop
          // at once. Anything else (network, a 5xx) gets a few more tries.
          var expired = err && err.category === "AUTH_REQUIRED";
          if (expired || ++errors >= MAX_CONSECUTIVE_ERRORS) {
            setStatus(lastReady ? "ready" : "failed");
            return "error";
          }
          await sleep(ERROR_DELAY_MS);
          continue;
        }
        var data = res && res.data;
        if (!data || data.enabled === false) {
          setStatus("unavailable");
          return "disabled";
        }
        if (data.error) {
          if (data.ready) {                 // the old index is still usable
            setStatus("ready");
            return "error";
          }
          if (++errors >= MAX_CONSECUTIVE_ERRORS) {
            setStatus("failed");
            return "error";
          }
          await sleep(ERROR_DELAY_MS);
          continue;
        }
        errors = 0;
        if (data.ready) {
          setStatus("ready");
          return "ready";
        }
        // The backend confirmed it is not usable (yet, or any more, e.g. it
        // expired): a build is under way, so callers should wait.
        lastReady = false;
        setStatus("warming");
        await sleep(ROUND_DELAY_MS);
      }
      setStatus("failed");
      return "gave_up";
    } finally {
      running = false;
    }
  }

  // True only once the backend has CONFIRMED a build is in progress (the index
  // is not usable yet). While the first check is still in flight it is false:
  // see isChecking().
  function isBuilding() {
    return status === "warming";
  }

  // True while the first check is in flight and nobody knows yet whether the
  // index is usable.
  function isChecking() {
    return status === "checking";
  }

  // A promise on `list` that resolves with the value passed to flush(), or with
  // false after `timeoutMs`. Never rejects.
  function waitOn(list, timeoutMs) {
    return new Promise(function (resolve) {
      var settled = false;
      var timer = null;
      function finish(value) {
        if (settled) return;
        settled = true;
        if (timer !== null && typeof clearTimeout === "function") clearTimeout(timer);
        resolve(value);
      }
      list.push(finish);
      if (timeoutMs > 0) timer = setTimeout(function () { finish(false); }, timeoutMs);
    });
  }

  // Resolves true once the index is ready, or false if the build fails, nothing
  // is checking or building, or `timeoutMs` passes first.
  function whenReady(timeoutMs) {
    if (status === "ready") return Promise.resolve(true);
    if (status !== "warming" && status !== "checking") return Promise.resolve(false);
    return waitOn(readyWaiters, timeoutMs);
  }

  // Resolves true once the first check has been answered (whatever the answer),
  // at once if no check is in flight, or false after `timeoutMs`.
  function whenKnown(timeoutMs) {
    if (status !== "checking") return Promise.resolve(true);
    return waitOn(knownWaiters, timeoutMs);
  }

  // Begin warming now and re-check periodically while the page stays open.
  function start() {
    warm();
    setInterval(function () { warm(); }, RECHECK_MS);
  }

  window.Catalog = {
    warm: warm,
    start: start,
    isBuilding: isBuilding,
    isChecking: isChecking,
    whenReady: whenReady,
    whenKnown: whenKnown,
    state: function () { return status; },
  };
})();
