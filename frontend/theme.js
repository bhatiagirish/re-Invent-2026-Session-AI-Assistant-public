/* theme.js — dark/light theme toggle.
 *
 * Default is DARK (Option A): the app loads dark unless the user has previously
 * chosen light. The choice is remembered in localStorage. The theme is applied
 * by setting <html data-theme="dark"|"light">; the light look is the CSS default
 * (theme-dark.css only styles [data-theme="dark"]), so "light" simply removes
 * the dark rules.
 *
 * To avoid a flash of the wrong theme, applyInitial() runs as early as possible
 * (call it inline in <head> if desired); wire() attaches the toggle button.
 */
(function () {
  var KEY = "reinvent-theme";          // "dark" | "light"
  var root = document.documentElement;

  function stored() {
    try { return localStorage.getItem(KEY); } catch (_) { return null; }
  }
  function persist(v) {
    try { localStorage.setItem(KEY, v); } catch (_) {}
  }

  // Resolve the active theme: stored choice if present, else DARK by default.
  function current() {
    var s = stored();
    return s === "light" || s === "dark" ? s : "dark";
  }

  function apply(theme) {
    root.setAttribute("data-theme", theme);
    updateButton(theme);
  }

  // Apply the initial theme ASAP (before paint if called early enough).
  function applyInitial() {
    root.setAttribute("data-theme", current());
  }

  function toggle() {
    var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    persist(next);
    apply(next);
  }

  function updateButton(theme) {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    var dark = theme === "dark";
    // Icon-only control: show the icon of the theme you'll switch TO on click.
    // The accessible name + tooltip carry the meaning (no visible text label).
    btn.textContent = dark ? "\u2600\uFE0E" : "\u263E";   // ☀︎ (to light) / ☾ (to dark)
    btn.setAttribute("aria-pressed", dark ? "true" : "false");
    btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
    btn.title = dark ? "Switch to light theme" : "Switch to dark theme";
  }

  function wire() {
    applyInitial();
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.addEventListener("click", toggle);
    updateButton(current());
  }

  // Expose for app bootstrap + apply initial immediately on load.
  window.Theme = { wire, toggle, applyInitial, current };
  applyInitial();
})();
