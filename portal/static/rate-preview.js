// Live "what would a 100-card list cost" preview next to any form carrying
// the three rate fields (admin/settings.html, admin/dashboard.html's round
// forms). Self-hosted, no dependencies -- the CSP is script-src 'self' with
// no inline scripts allowed, so this has to be its own file (see
// deploy/Caddyfile). Purely cosmetic: the server-side price computation in
// portal/pricing.py is the actual source of truth, this only previews it.
(function () {
  "use strict";

  const EXAMPLE_CARDS = 100;
  const EXAMPLE_LISTS = 1;

  function formatCents(cents) {
    const sign = cents < 0 ? "-" : "";
    cents = Math.abs(Math.round(cents));
    const euros = Math.floor(cents / 100);
    const rest = String(cents % 100).padStart(2, "0");
    return sign + euros + "," + rest + " €";
  }

  function wireUp(root) {
    const perCard = root.querySelector("[data-rate-per-card]");
    const perList = root.querySelector("[data-rate-per-list]");
    const fee = root.querySelector("[data-rate-fee]");
    const preview = root.querySelector("[data-rate-preview]");
    if (!perCard || !perList || !fee || !preview) return;

    function update() {
      const c = parseInt(perCard.value, 10) || 0;
      const l = parseInt(perList.value, 10) || 0;
      const f = parseInt(fee.value, 10) || 0;
      const total = EXAMPLE_CARDS * c + EXAMPLE_LISTS * l + f;
      preview.textContent =
        "Beispiel: " + EXAMPLE_CARDS + " Karten, " + EXAMPLE_LISTS + " Liste → " + formatCents(total);
    }

    [perCard, perList, fee].forEach(function (el) {
      el.addEventListener("input", update);
    });
    update();
  }

  document.querySelectorAll("form").forEach(wireUp);
})();
