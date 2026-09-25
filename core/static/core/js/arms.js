/* =====================================================================
   ARMS — progressive enhancement only.
   Every template works without this file; it adds three behaviours:
     1. mobile navigation toggle (aria-expanded driven)
     2. dismissible flash messages
     3. confirmation prompts for destructive forms via data-confirm
   No dependencies, runs on defer, safe to load on every page.
   ===================================================================== */
(function () {
  "use strict";

  /* ---- 1. mobile navigation toggle -------------------------------- */
  function initNavToggle() {
    var toggle = document.querySelector(".nav-toggle");
    if (!toggle) return;

    var nav = document.getElementById(toggle.getAttribute("aria-controls")) ||
              document.querySelector(".topnav");
    if (!nav) return;

    toggle.addEventListener("click", function () {
      var open = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", String(!open));
      nav.classList.toggle("is-open", !open);
    });

    /* Escape closes the menu and returns focus to the toggle */
    nav.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      toggle.setAttribute("aria-expanded", "false");
      nav.classList.remove("is-open");
      toggle.focus();
    });

    /* clicking a link collapses the menu again on small screens */
    nav.addEventListener("click", function (event) {
      if (!event.target.closest("a")) return;
      toggle.setAttribute("aria-expanded", "false");
      nav.classList.remove("is-open");
    });
  }

  /* ---- 2. dismissible flash messages ------------------------------ */
  function initMessages() {
    var buttons = document.querySelectorAll(".messages__close");
    if (!buttons.length) return;

    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function () {
        var item = button.closest(".messages__item");
        if (!item) return;

        item.style.transition = "opacity .18s ease";
        item.style.opacity = "0";

        /* remove from the DOM after the fade so the live region clears */
        window.setTimeout(function () {
          if (item.parentNode) item.parentNode.removeChild(item);
        }, 180);
      });
    });
  }

  /* ---- 3. confirmation for destructive actions -------------------- */
  /* Markup carries data-confirm="..." on the <form>; the browser's own
     confirm() is used so nothing is lost when JS is unavailable in the
     sense that the action simply proceeds, exactly as a plain form.   */
  function initConfirmForms() {
    document.addEventListener("submit", function (event) {
      var form = event.target.closest ? event.target.closest("form[data-confirm]") : null;
      if (!form) return;
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
        event.stopPropagation();
      }
    });

    /* a submit button pressed programmatically bypasses the form's own
       submit event in some browsers; guard those too */
    document.addEventListener("click", function (event) {
      var button = event.target.closest ?
        event.target.closest("button[data-confirm], input[data-confirm]") : null;
      if (!button) return;
      if (!window.confirm(button.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    });
  }

  /* ---- run when the DOM is ready ---------------------------------- */
  function ready(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  ready(function () {
    initNavToggle();
    initMessages();
    initConfirmForms();
  });
})();
