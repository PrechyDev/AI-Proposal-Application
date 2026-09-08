(function () {
  // Instant feedback for real page navigations (this app has no SPA/HTMX
  // layer on most pages - every nav link and button is a full page load,
  // which can take a couple of real seconds against this app's DB). A
  // slim top progress bar starts filling the instant a same-origin link
  // or form is activated, so a click never looks like it did nothing.
  var bar = document.createElement("div");
  bar.id = "nav-progress-bar";
  document.documentElement.appendChild(bar);

  function startProgress() {
    document.documentElement.classList.add("is-navigating");
  }

  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest("a[href]");
    if (!link || link.target === "_blank" || link.hasAttribute("download")) return;
    var href = link.getAttribute("href");
    if (!href || href.startsWith("#") || href.startsWith("mailto:") || href.startsWith("tel:")) return;
    var url;
    try {
      url = new URL(link.href, window.location.href);
    } catch (err) {
      return;
    }
    if (url.origin !== window.location.origin) return;
    startProgress();
  });

  document.addEventListener("submit", function (event) {
    if (!event.defaultPrevented) startProgress();
  });

  // A bfcache-restored page (browser "back") should never show a stale bar.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      document.documentElement.classList.remove("is-navigating");
    }
  });
})();
