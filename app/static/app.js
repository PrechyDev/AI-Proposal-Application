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

  // Password show/hide toggle - any <button class="password-toggle"
  // data-target="<input id>"> gets this for free, no per-page script
  // needed (originally written once for login.html, now shared since
  // accept-invite/reset-password repeat the same field).
  document.querySelectorAll(".password-toggle").forEach(function (toggleBtn) {
    var input = document.getElementById(toggleBtn.dataset.target);
    if (!input) return;
    toggleBtn.addEventListener("click", function () {
      var showing = input.type === "text";
      input.type = showing ? "password" : "text";
      toggleBtn.classList.toggle("is-visible", !showing);
      toggleBtn.setAttribute("aria-label", showing ? "Show password" : "Hide password");
    });
  });

  // Submit-button loading state - any <button type="submit"
  // data-loading-text="..."> is disabled with a spinner and swapped text
  // the instant its form submits (same idea as the top progress bar, but
  // for the one control the user actually clicked). Needs its own
  // <span class="spinner" hidden> and a text span carrying [data-btn-text].
  document.querySelectorAll("button[type=submit][data-loading-text]").forEach(function (submitBtn) {
    var form = submitBtn.closest("form");
    if (!form) return;
    var spinner = submitBtn.querySelector(".spinner");
    var textEl = submitBtn.querySelector("[data-btn-text]");
    var originalText = textEl ? textEl.textContent : null;
    form.addEventListener("submit", function () {
      submitBtn.disabled = true;
      if (spinner) spinner.hidden = false;
      if (textEl) textEl.textContent = submitBtn.dataset.loadingText;
    });
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) {
        submitBtn.disabled = false;
        if (spinner) spinner.hidden = true;
        if (textEl && originalText !== null) textEl.textContent = originalText;
      }
    });
  });
})();
