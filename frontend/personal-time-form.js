/* personal-time-form.js — add / edit / delete form for personal-time blocks
 * (Req 18, 19, 20, 23.4, 23.5).
 *
 * Renders a single form that works in two modes:
 *   • add  — create a new personal-time block via window.ScheduleActions.create
 *   • edit — pre-filled from an existing personalTime item, saved via
 *            window.ScheduleActions.update; also exposes a Delete action wired
 *            to window.ScheduleActions.remove.
 *
 * Fields: title (required, ≤200), start, end (Pacific event-local, entered via
 * datetime-local → "YYYY-MM-DDTHH:MM"), description (optional, ≤1000),
 * location (optional, ≤200).
 *
 * Client-side validation MIRRORS the backend contract (backend/schedule.py)
 * and runs BEFORE any API call — it is UX, not the security boundary; the
 * backend re-validates. Rules:
 *   • title / start / end required (names each empty field)       — Req 18.4
 *   • title ≤200, description ≤1000, location ≤200                — Req 18.5
 *   • start / end on whole minutes (seconds == 00)                — Req 18.2/18.3
 *   • end strictly after start                                    — Req 18.2/18.3
 *   • duration a positive whole multiple of 5 minutes             — Req 18.2/18.3
 * On invalid input the form shows inline messages, names the violated field,
 * retains the entered values, and does NOT submit (Req 18.3/18.4).
 *
 * Submit → ScheduleActions.create/update; Delete → ScheduleActions.remove. The
 * Schedule_View is refreshed only on a confirmed action.status === "success"
 * (ScheduleView already does that on success). On a failed/validation action
 * from the backend the form stays open and populated for retry (the backend
 * returns retainedInput). The browser talks ONLY to the application backend.
 */
(function () {
  // Length caps — must match backend/config.PERSONAL_TIME_*_MAX_LEN.
  var TITLE_MAX = 200;
  var DESCRIPTION_MAX = 1000;
  var LOCATION_MAX = 200;
  var INCREMENT_MINUTES = 5;

  function esc(s) {
    if (window.Sessions && typeof window.Sessions.esc === "function") {
      return window.Sessions.esc(s);
    }
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ── Datetime helpers ────────────────────────────────────────────────────────
  // A datetime-local control yields "YYYY-MM-DDTHH:MM" (whole minutes). We also
  // tolerate a trailing ":SS" if the browser supplies seconds. These values are
  // Pacific event-local and are sent to the backend verbatim; the backend does
  // the Pacific→UTC conversion (never a hardcoded offset).
  function parseLocalParts(value) {
    if (typeof value !== "string") return null;
    var m = value.trim().match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/);
    if (!m) return null;
    return {
      year: +m[1], month: +m[2], day: +m[3],
      hour: +m[4], minute: +m[5], second: +(m[6] || 0),
    };
  }

  // Minutes-since-epoch in a fixed (UTC) frame. Both start and end are read in
  // the same frame, so the difference is the true wall-clock duration for any
  // date that is NOT a DST transition. (The authoritative check is server-side,
  // which uses the real IANA rule; this is the UX pre-check.)
  function localMinutes(p) {
    return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute) / 60000;
  }

  // ── Validation ───────────────────────────────────────────────────────────────
  // Returns { ok, errors } where errors maps field → message. Field names match
  // the input ids so the UI can mark exactly the violated field (Req 18.3/18.4).
  function validate(values) {
    var errors = {};
    var title = (values.title || "").trim();
    var start = (values.start || "").trim();
    var end = (values.end || "").trim();
    var description = values.description || "";
    var location = values.location || "";

    // Required fields (Req 18.4) — name each empty required field.
    if (!title) errors.title = "Title is required.";
    if (!start) errors.start = "Start time is required.";
    if (!end) errors.end = "End time is required.";

    // Length caps (Req 18.5) — identify field + maximum length.
    if (title.length > TITLE_MAX) {
      errors.title = "Title must be " + TITLE_MAX + " characters or fewer.";
    }
    if (description.length > DESCRIPTION_MAX) {
      errors.description = "Description must be " + DESCRIPTION_MAX + " characters or fewer.";
    }
    if (location.length > LOCATION_MAX) {
      errors.location = "Location must be " + LOCATION_MAX + " characters or fewer.";
    }

    // Datetime constraints only when both are present and well-formed.
    var sp = start ? parseLocalParts(start) : null;
    var ep = end ? parseLocalParts(end) : null;
    if (start && !sp) errors.start = "Start time must look like YYYY-MM-DDTHH:MM.";
    if (end && !ep) errors.end = "End time must look like YYYY-MM-DDTHH:MM.";

    // Seconds must be 00 — whole minutes (Req 18.2/18.3).
    if (sp && sp.second !== 0) errors.start = "Start time must be on a whole minute (seconds must be 00).";
    if (ep && ep.second !== 0) errors.end = "End time must be on a whole minute (seconds must be 00).";

    // end strictly after start + positive whole 5-minute multiple (Req 18.2/18.3).
    if (sp && ep && !errors.start && !errors.end) {
      var duration = localMinutes(ep) - localMinutes(sp);
      if (duration <= 0) {
        errors.end = "End time must be after the start time.";
      } else if (duration % INCREMENT_MINUTES !== 0) {
        errors.end = "Duration must be a whole number of " + INCREMENT_MINUTES + "-minute increments.";
      }
    }

    return { ok: Object.keys(errors).length === 0, errors: errors };
  }

  // ── DOM construction ──────────────────────────────────────────────────────────
  function field(id, label, type, opts) {
    opts = opts || {};
    var wrap = document.createElement("div");
    wrap.className = "pt-field";
    var inputTag = opts.multiline ? "textarea" : "input";
    var attrs = opts.multiline ? "" : ' type="' + type + '"';
    var maxAttr = opts.maxLength ? ' maxlength="' + opts.maxLength + '"' : "";
    var reqAttr = opts.required ? ' aria-required="true"' : "";
    wrap.innerHTML =
      '<label class="pt-label" for="' + id + '">' + esc(label) +
      (opts.required ? ' <span class="pt-req" aria-hidden="true">*</span>' : "") +
      "</label>" +
      "<" + inputTag + ' id="' + id + '" class="pt-input"' + attrs + maxAttr + reqAttr +
      ' aria-describedby="' + id + '-err"></' + inputTag + ">" +
      '<p class="pt-error" id="' + id + '-err" role="alert" hidden></p>';
    return wrap;
  }

  // Build the form element. `mode` is "add" | "edit"; `item` is the existing
  // personalTime object (edit mode) carrying personalTimeId + current values.
  function buildForm(mode, item) {
    item = item || {};
    var form = document.createElement("form");
    form.className = "pt-form";
    form.setAttribute("novalidate", "novalidate");
    form.dataset.mode = mode;
    if (item.personalTimeId) form.dataset.personalTimeId = item.personalTimeId;

    var heading = document.createElement("h3");
    heading.className = "pt-form-title";
    heading.textContent = mode === "edit" ? "Edit personal time" : "Add personal time";
    form.appendChild(heading);

    form.appendChild(field("pt-title", "Title", "text", { required: true, maxLength: TITLE_MAX }));
    form.appendChild(field("pt-start", "Start (Pacific)", "datetime-local", { required: true }));
    form.appendChild(field("pt-end", "End (Pacific)", "datetime-local", { required: true }));
    form.appendChild(field("pt-location", "Location", "text", { maxLength: LOCATION_MAX }));
    form.appendChild(field("pt-description", "Description", "text", { multiline: true, maxLength: DESCRIPTION_MAX }));

    // A non-field-specific error line (backend failure / timeout messages).
    var formErr = document.createElement("p");
    formErr.className = "pt-form-error";
    formErr.id = "pt-form-error";
    formErr.setAttribute("role", "alert");
    formErr.hidden = true;
    form.appendChild(formErr);

    var actions = document.createElement("div");
    actions.className = "pt-actions";
    actions.innerHTML =
      '<button type="submit" class="btn btn-primary pt-submit">' +
      (mode === "edit" ? "Save changes" : "Add to schedule") + "</button>" +
      '<button type="button" class="btn btn-secondary pt-cancel">Cancel</button>' +
      (mode === "edit"
        ? '<button type="button" class="btn btn-danger pt-delete">Delete</button>'
        : "");
    form.appendChild(actions);

    // Pre-fill (edit mode). Pacific-local start/end come either from retained
    // input ("YYYY-MM-DDTHH:MM" as entered) or must be derived from the UTC
    // contract form the Schedule_View carries; the opener passes already-local
    // values, so set them verbatim.
    setValue(form, "pt-title", item.title);
    setValue(form, "pt-start", item.start);
    setValue(form, "pt-end", item.end);
    setValue(form, "pt-location", item.location);
    setValue(form, "pt-description", item.description);

    return form;
  }

  function setValue(form, id, value) {
    var elm = form.querySelector("#" + id);
    if (elm) elm.value = value == null ? "" : String(value);
  }
  function getValue(form, id) {
    var elm = form.querySelector("#" + id);
    return elm ? elm.value : "";
  }

  function readValues(form) {
    return {
      title: getValue(form, "pt-title"),
      start: getValue(form, "pt-start"),
      end: getValue(form, "pt-end"),
      location: getValue(form, "pt-location"),
      description: getValue(form, "pt-description"),
    };
  }

  // Clear every inline error, then show the ones in `errors` (field → message).
  function showErrors(form, errors) {
    ["pt-title", "pt-start", "pt-end", "pt-location", "pt-description"].forEach(function (id) {
      var errEl = form.querySelector("#" + id + "-err");
      var inputEl = form.querySelector("#" + id);
      var key = id.replace("pt-", "");
      if (errors && errors[key]) {
        if (errEl) { errEl.textContent = errors[key]; errEl.hidden = false; }
        if (inputEl) { inputEl.classList.add("pt-invalid"); inputEl.setAttribute("aria-invalid", "true"); }
      } else {
        if (errEl) { errEl.textContent = ""; errEl.hidden = true; }
        if (inputEl) { inputEl.classList.remove("pt-invalid"); inputEl.removeAttribute("aria-invalid"); }
      }
    });
  }

  function showFormError(form, message) {
    var el = form.querySelector("#pt-form-error");
    if (!el) return;
    if (message) { el.textContent = message; el.hidden = false; }
    else { el.textContent = ""; el.hidden = true; }
  }

  function setBusy(form, busy) {
    var submit = form.querySelector(".pt-submit");
    var del = form.querySelector(".pt-delete");
    if (submit) submit.disabled = !!busy;
    if (del) del.disabled = !!busy;
    form.classList.toggle("pt-busy", !!busy);
  }

  // ── Submit / delete orchestration ────────────────────────────────────────────
  // onDone(result) is called after a confirmed success so the opener can close
  // the form; on failure the form stays populated for retry.
  async function handleSubmit(form, onDone) {
    showFormError(form, "");
    var values = readValues(form);
    var result = validate(values);
    showErrors(form, result.errors);
    if (!result.ok) return; // Do NOT submit while invalid (Req 18.3/18.4).

    var actions = window.ScheduleActions;
    if (!actions) { showFormError(form, "The schedule service is unavailable."); return; }

    var mode = form.dataset.mode;
    var id = form.dataset.personalTimeId;
    var entry = {
      title: values.title.trim(),
      start: values.start.trim(),
      end: values.end.trim(),
      description: values.description || "",
      location: values.location || "",
    };

    setBusy(form, true);
    var action;
    try {
      action = mode === "edit" && id
        ? await actions.update(id, entry)
        : await actions.create(entry);
    } catch (err) {
      setBusy(form, false);
      showFormError(form, "The personal time block could not be saved. Please try again.");
      return;
    }
    setBusy(form, false);

    if (action && action.status === "success") {
      if (typeof onDone === "function") onDone(action);
      return;
    }

    // Failed / not-found / validation from the backend: keep the form populated
    // for retry. Re-apply any retainedInput the backend echoed (Req 18.8/19.5).
    var retained = action && action.retainedInput;
    if (retained) {
      setValue(form, "pt-title", retained.title);
      setValue(form, "pt-start", retained.start);
      setValue(form, "pt-end", retained.end);
      setValue(form, "pt-location", retained.location);
      setValue(form, "pt-description", retained.description);
    }
    showFormError(form, (action && action.message) ||
      "The personal time block could not be saved. Please try again.");
  }

  async function handleDelete(form, onDone) {
    var id = form.dataset.personalTimeId;
    if (!id) return;
    var actions = window.ScheduleActions;
    if (!actions) { showFormError(form, "The schedule service is unavailable."); return; }
    showFormError(form, "");
    setBusy(form, true);
    var action;
    try {
      action = await actions.remove(id);
    } catch (err) {
      setBusy(form, false);
      showFormError(form, "The personal time block could not be deleted. Please try again.");
      return;
    }
    setBusy(form, false);
    if (action && action.status === "success") {
      if (typeof onDone === "function") onDone(action);
      return;
    }
    showFormError(form, (action && action.message) ||
      "The personal time block could not be deleted. Please try again.");
  }

  // ── Public open() ──────────────────────────────────────────────────────────
  // Renders the form into `container`, wires its buttons, and returns the form
  // element. opts: { mode, item, onDone, onCancel }.
  function open(container, opts) {
    opts = opts || {};
    var form = buildForm(opts.mode || "add", opts.item);

    form.addEventListener("submit", function (ev) {
      if (ev && typeof ev.preventDefault === "function") ev.preventDefault();
      handleSubmit(form, opts.onDone);
    });
    var cancel = form.querySelector(".pt-cancel");
    if (cancel) cancel.addEventListener("click", function () {
      if (typeof opts.onCancel === "function") opts.onCancel();
    });
    var del = form.querySelector(".pt-delete");
    if (del) del.addEventListener("click", function () {
      handleDelete(form, opts.onDone);
    });

    if (container) {
      container.innerHTML = "";
      container.appendChild(form);
      var firstInput = form.querySelector("#pt-title");
      if (firstInput && typeof firstInput.focus === "function") firstInput.focus();
    }
    return form;
  }

  window.PersonalTimeForm = {
    open: open,
    // Exposed for unit tests (10.6) and reuse.
    validate: validate,
    buildForm: buildForm,
    readValues: readValues,
  };
})();
