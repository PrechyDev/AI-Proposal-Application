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
  // the instant its form submits. Needs its own <span class="spinner"
  // hidden> and a text span carrying [data-btn-text].
  //
  // Deliberately keyed off `event.submitter` (the actual button that
  // triggered the SubmitEvent), not just "this form submitted" - a page
  // can have several submit buttons sharing one <form> on purpose (e.g.
  // the New Proposal page: intake fields + several reference-file actions
  // all live in one form so typed-in values survive a sub-action via
  // formaction overrides). Reacting to any submit regardless of which
  // button triggered it was a real bug: clicking "Upload and Attach" also
  // showed the unrelated "Create Proposal" button's own spinner. Every
  // OTHER submit button in that same form is also disabled for the
  // duration of the request (whether or not it has its own loading text),
  // so a second action can't be fired while the first is still in flight.
  var loadingButtons = new Map();
  document.querySelectorAll("button[type=submit][data-loading-text]").forEach(function (submitBtn) {
    loadingButtons.set(submitBtn, {
      spinner: submitBtn.querySelector(".spinner"),
      textEl: submitBtn.querySelector("[data-btn-text]"),
      originalText: (function () {
        var textEl = submitBtn.querySelector("[data-btn-text]");
        return textEl ? textEl.textContent : null;
      })(),
    });
  });

  document.addEventListener("submit", function (event) {
    if (event.defaultPrevented) return;
    startProgress();

    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    var submitter = event.submitter;

    form.querySelectorAll("button[type=submit]").forEach(function (btn) {
      btn.disabled = true;
    });

    if (submitter && loadingButtons.has(submitter)) {
      var state = loadingButtons.get(submitter);
      if (state.spinner) state.spinner.hidden = false;
      if (state.textEl) state.textEl.textContent = submitter.dataset.loadingText;
    }
  });

  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    document.querySelectorAll("button[type=submit]").forEach(function (btn) {
      btn.disabled = false;
    });
    loadingButtons.forEach(function (state) {
      if (state.spinner) state.spinner.hidden = true;
      if (state.textEl && state.originalText !== null) state.textEl.textContent = state.originalText;
    });
  });

  // A form with more than one submit button (formaction overrides send a
  // specific button's click to a specific sub-action - see the loading-
  // state comment above) has no single "right" implicit submit target.
  // Pressing Enter in a text field still triggers the browser's default
  // implicit-submission behavior, which always picks the FIRST submit
  // button in DOM order regardless of which action the user actually
  // meant - a real, confirmed bug: typing a tag into an upload row and
  // hitting Enter silently submitted to "Attach Selected" (the first
  // button in the New Proposal page's shared form) instead of "Upload
  // and Attach", discarding the file the user had just picked with no
  // visible error. Disable Enter-triggered implicit submission entirely
  // on any form with 2+ submit buttons - every action there must be an
  // explicit click.
  document.querySelectorAll("form").forEach(function (form) {
    if (form.querySelectorAll("button[type=submit]").length < 2) return;
    form.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      var el = event.target;
      if (el.tagName === "INPUT" && el.type !== "submit" && el.type !== "button") {
        event.preventDefault();
      }
    });
  });

  // Accumulating multi-file picker - a plain <input type="file" multiple>
  // replaces its own FileList on every pick, so selecting one file, then
  // opening the picker again to add one more, would normally throw away
  // the first choice. This keeps a running JS-side list across repeated
  // picks and re-syncs the real <input> (via DataTransfer) so the actual
  // form submission still carries every staged file - shared by the
  // reference library's upload form and the New Proposal page's
  // upload-from-device panel, one row (Name/Description/Tags, plus an
  // optional per-row "add to library" checkbox) per staged file.
  //
  // Also mirrors the backend's own extension allowlist (app/services/
  // reference_files.py) client-side, and supports drag-and-drop onto an
  // optional dropzone element - an unsupported file used to only get
  // caught after a full round trip to the server, with the rejection
  // reported as a single line at the top of the page; a user who had
  // scrolled down to the reference section (as anyone filling this
  // multi-file, multi-field form naturally would) could easily miss it,
  // then go on to click Create Proposal anyway since the intake fields
  // were already valid - producing a proposal with the reference they
  // wanted silently missing. Rejecting client-side, right next to the
  // picker, closes that gap.
  var REFERENCE_FILE_ALLOWED_EXTENSIONS = [".txt", ".md", ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp"];

  function extensionOf(filename) {
    var i = filename.lastIndexOf(".");
    return i === -1 ? "" : filename.slice(i).toLowerCase();
  }

  function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  window.setupAccumulatingFileInput = function (config) {
    var fileInput = document.getElementById(config.inputId);
    var rowsContainer = document.getElementById(config.rowsContainerId);
    var dropzone = config.dropzoneId ? document.getElementById(config.dropzoneId) : null;
    var errorBox = config.errorContainerId ? document.getElementById(config.errorContainerId) : null;
    var uploadButton = config.uploadButtonId ? document.getElementById(config.uploadButtonId) : null;
    if (!fileInput || !rowsContainer) return;
    var staged = [];

    // A file staged here isn't uploaded until this button is actually
    // clicked - highlighting it the moment there's something waiting is a
    // visible cue that clicking "Create Proposal"/away from this page
    // would otherwise silently leave these files behind (see the
    // unattached-files-modal warning, which catches this at submit time -
    // this is the proactive version of the same thing).
    function refreshUploadButtonHighlight() {
      if (!uploadButton) return;
      uploadButton.classList.toggle("btn-upload-ready", staged.length > 0);
    }

    function syncInputFiles() {
      var dt = new DataTransfer();
      staged.forEach(function (file) {
        dt.items.add(file);
      });
      fileInput.files = dt.files;
    }

    function render() {
      refreshUploadButtonHighlight();
      // Staged-file removal below rewrites fileInput.files programmatically
      // (syncInputFiles), which never fires a native "change" event - this
      // callback is the only reliable hook for a caller that needs to react
      // to the staged list changing on both add AND remove.
      if (typeof config.onStagedChange === "function") config.onStagedChange();
      rowsContainer.innerHTML = "";
      staged.forEach(function (file, index) {
        var row = document.createElement("div");
        row.className = "upload-row";
        var safeName = file.name.replace(/"/g, "&quot;");
        var addToLibraryHtml = config.includeAddToLibrary
          ? '<label class="upload-row-checkbox"><input type="checkbox" name="add_to_library_indices" value="' +
            index +
            '"> Also add to library</label>'
          : "";
        row.innerHTML =
          '<div class="upload-row-file">' +
          '<span class="upload-row-filename">' + safeName + "</span>" +
          '<span class="upload-row-filesize">' + formatFileSize(file.size) + "</span>" +
          '<button type="button" class="upload-row-remove" aria-label="Remove this file">&times;</button>' +
          "</div>" +
          '<div class="upload-row-fields">' +
          '<div class="field"><label>Name</label><input type="text" name="names" value="' +
          safeName +
          '" required></div>' +
          '<div class="field"><label>Description</label><input type="text" name="descriptions"></div>' +
          '<div class="field"><label>Tags</label><input type="text" name="tags" placeholder="comma-separated"></div>' +
          addToLibraryHtml +
          "</div>";
        row.querySelector(".upload-row-remove").addEventListener("click", function () {
          staged.splice(index, 1);
          syncInputFiles();
          render();
        });
        rowsContainer.appendChild(row);
      });
    }

    function renderRejected(rejectedNames) {
      if (!errorBox) return;
      if (!rejectedNames.length) {
        errorBox.hidden = true;
        errorBox.textContent = "";
        return;
      }
      errorBox.hidden = false;
      errorBox.textContent =
        "Not added (unsupported file type): " + rejectedNames.join(", ") +
        ". Accepted types: " + REFERENCE_FILE_ALLOWED_EXTENSIONS.join(", ") + ".";
    }

    function addFiles(fileList) {
      var rejected = [];
      Array.prototype.forEach.call(fileList, function (file) {
        if (REFERENCE_FILE_ALLOWED_EXTENSIONS.indexOf(extensionOf(file.name)) === -1) {
          rejected.push(file.name);
          return;
        }
        staged.push(file);
      });
      syncInputFiles();
      render();
      renderRejected(rejected);
    }

    fileInput.addEventListener("change", function () {
      addFiles(fileInput.files);
    });

    if (dropzone) {
      dropzone.addEventListener("click", function () {
        fileInput.click();
      });
      dropzone.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          fileInput.click();
        }
      });
      dropzone.addEventListener("dragover", function (event) {
        event.preventDefault();
        dropzone.classList.add("is-dragover");
      });
      dropzone.addEventListener("dragleave", function () {
        dropzone.classList.remove("is-dragover");
      });
      dropzone.addEventListener("drop", function (event) {
        event.preventDefault();
        dropzone.classList.remove("is-dragover");
        if (event.dataTransfer && event.dataTransfer.files.length) {
          addFiles(event.dataTransfer.files);
        }
      });
    }
  };
})();
