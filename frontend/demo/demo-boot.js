/* demo-boot.js — small demo-only tweaks, loaded after the real app.js:
 *   - relabel the connection indicator ("Demo mode") instead of "Connected to
 *     re:Invent 2026", which would be misleading with sample data;
 *   - wire the "Reset demo" button (a reload restores the starting state). */
(function () {
  "use strict";

  var LABEL = "Demo mode (sample data)";
  var text = document.getElementById("conn-text");

  function relabel() {
    if (text && text.textContent !== LABEL) text.textContent = LABEL;
  }

  if (text) {
    // auth.js rewrites this text on every status check; keep our label.
    new MutationObserver(relabel).observe(text, {
      childList: true, characterData: true, subtree: true,
    });
    relabel();
  }

  var reset = document.getElementById("demo-reset");
  if (reset) {
    reset.addEventListener("click", function () { window.location.reload(); });
  }
})();
