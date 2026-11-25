// Small things that forms do.
// A row that has just been added to a list of values or of nodes (the server marks it
// with data-autofocus in what it answers to the Add button) gets the focus: its first
// field.
// The bars of the nodes can be used with the keyboard (see below).
// A submit button that is pressed is disabled and shows that the form is on its way (its
// icon is a spinner), so that it is not pressed twice; the page that answers is a new one.
(function () {
  document.addEventListener("htmx:afterSettle", () => {
    const row = document.querySelector("[data-autofocus]");
    if (!row) return;
    row.removeAttribute("data-autofocus");
    const field = row.querySelector("input:not([type=hidden]), select, textarea");
    if (field) field.focus();
  });

  // The bars of the nodes of a property, by the keyboard: Space and Enter open a bar (the
  // browser does that, and shuts the others), the arrow keys move to the next or the
  // previous bar, and Delete takes the node away (so does its x, which is the next stop).
  document.addEventListener("keydown", (event) => {
    const bar = event.target.closest && event.target.closest("summary.accordion-button");
    if (!bar || event.target !== bar) return;
    const bars = Array.from(bar.closest(".accordion").querySelectorAll(":scope > .accordion-item > details > summary"));
    const at = bars.indexOf(bar);
    if (event.key === "ArrowDown" && at < bars.length - 1) {
      event.preventDefault();
      bars[at + 1].focus();
    } else if (event.key === "ArrowUp" && at > 0) {
      event.preventDefault();
      bars[at - 1].focus();
    } else if (event.key === "Delete") {
      const remove = bar.closest(".accordion-item").querySelector(".bar-remove:not(:disabled)");
      if (remove) {
        event.preventDefault();
        remove.click();
      }
    }
  });

  const busy = (button, on) => {
    button.disabled = on;
    button.toggleAttribute("aria-busy", on);
    const icon = button.querySelector("i.bi, .spinner-border");
    if (!icon) return;
    if (on) {
      icon.dataset.was = icon.className;
      icon.className = "spinner-border spinner-border-sm me-1";
    } else if (icon.dataset.was) {
      icon.className = icon.dataset.was;
    }
  };

  document.addEventListener("submit", (event) => {
    const form = event.target;
    const button = event.submitter;
    if (!button || !form.matches("form.deform") || button.type !== "submit") return;
    // after the browser has taken the values of the form: a disabled button has none
    setTimeout(() => busy(button, true), 0);
  });

  // coming back to the page with the back button shows it as it was left
  window.addEventListener("pageshow", () => {
    for (const button of document.querySelectorAll("form.deform button[aria-busy]")) {
      busy(button, false);
    }
  });
})();
