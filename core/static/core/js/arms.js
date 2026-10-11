/* =====================================================================
   ARMS — progressive enhancement only.
   Every template works without this file; it adds four behaviours:
     1. mobile navigation toggle (aria-expanded driven)
     2. dismissible flash messages
     3. confirmation prompts for destructive forms via data-confirm
     4. dropdown menus for the navigation groups and the user menu
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

  /* ---- 4. dropdown menus ------------------------------------------ */
  /* Markup: an element with data-menu holding a <button aria-controls>
     and the menu it controls. A disclosure pattern rather than an ARIA
     "menu": the items are ordinary links, so screen readers announce
     them as such. Click toggles; Escape, an outside click or tabbing
     away closes; arrow keys, Home and End move between the items.
     Without JS the CSS opens a menu on hover or keyboard focus instead;
     the has-js class hands that over to aria-expanded.                */
  function initDropdowns() {
    var groups = Array.prototype.slice.call(document.querySelectorAll("[data-menu]"));
    if (!groups.length) return;
    document.documentElement.classList.add("has-js");

    function parts(group) {
      var button = group.querySelector("button[aria-controls]");
      var menu = button ? document.getElementById(button.getAttribute("aria-controls")) : null;
      return { button: button, menu: menu };
    }

    function items(menu) {
      return Array.prototype.slice.call(menu.querySelectorAll("a[href], button"));
    }

    function isOpen(group) {
      return parts(group).button.getAttribute("aria-expanded") === "true";
    }

    function setOpen(group, open) {
      parts(group).button.setAttribute("aria-expanded", String(open));
    }

    function closeAll(except) {
      groups.forEach(function (group) {
        if (group !== except) setOpen(group, false);
      });
    }

    groups = groups.filter(function (group) {
      var p = parts(group);
      return p.button && p.menu;
    });

    groups.forEach(function (group) {
      var p = parts(group);

      p.button.addEventListener("click", function () {
        var open = !isOpen(group);
        closeAll(group);
        setOpen(group, open);
      });

      /* ArrowDown / ArrowUp on the button open the menu at the first or
         last item */
      p.button.addEventListener("keydown", function (event) {
        if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
        event.preventDefault();
        closeAll(group);
        setOpen(group, true);
        var list = items(p.menu);
        var target = event.key === "ArrowDown" ? list[0] : list[list.length - 1];
        if (target) target.focus();
      });

      p.menu.addEventListener("keydown", function (event) {
        var list = items(p.menu);
        if (!list.length) return;
        var index = list.indexOf(document.activeElement);
        var next = null;
        if (event.key === "ArrowDown") next = list[(index + 1) % list.length];
        else if (event.key === "ArrowUp") next = list[(index - 1 + list.length) % list.length];
        else if (event.key === "Home") next = list[0];
        else if (event.key === "End") next = list[list.length - 1];
        if (!next) return;
        event.preventDefault();
        next.focus();
      });

      /* Escape closes this menu and returns focus to its button; it is
         stopped here so the mobile navigation does not also collapse */
      group.addEventListener("keydown", function (event) {
        if (event.key !== "Escape" || !isOpen(group)) return;
        event.stopPropagation();
        setOpen(group, false);
        p.button.focus();
      });

      /* tabbing out of the group closes it */
      group.addEventListener("focusout", function (event) {
        if (event.relatedTarget && !group.contains(event.relatedTarget)) {
          setOpen(group, false);
        }
      });
    });

    document.addEventListener("click", function (event) {
      groups.forEach(function (group) {
        if (!group.contains(event.target)) setOpen(group, false);
      });
    });

    /* Escape also closes a menu that was opened with the mouse while
       focus is elsewhere on the page */
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeAll(null);
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
    initDropdowns();
  });
})();
