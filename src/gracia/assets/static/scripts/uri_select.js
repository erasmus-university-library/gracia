// A URISelectWidget: a Bootstrap dropdown that is a choice of URIs. The item that is clicked is
// the value (the hidden input, which is what is posted), the label and icon of the button, and
// the one that has the check mark; the hidden input says `input` (the bar of a node that has
// the choice in its text, template.js) and `change` (the tab named after the class that is
// chosen, form.js). It has the label of the choice too (data-label), for the bar.
//
// A choice that is only for some resources (data-only-for: their rdf classes, as the
// thesis advisor is for a thesis) is left out of the menu of the others: what the type of the
// resource says is the field `type` of the form.
document.addEventListener("click", (event) => {
  const item = event.target.closest && event.target.closest(".uri-select .dropdown-item");
  const select = item && item.closest(".uri-select");
  if (!select) return;
  const value = select.querySelector(".uri-select-value");
  value.value = item.dataset.value;
  if (item.dataset.value) value.dataset.label = item.dataset.label;
  else delete value.dataset.label;
  select.querySelector(".uri-select-label").textContent = item.dataset.label;
  const toggle = select.querySelector(".uri-select-toggle");
  let icon = toggle.querySelector("i.bi");
  if (item.dataset.icon) {
    if (!icon) {
      icon = document.createElement("i");
      icon.setAttribute("aria-hidden", "true");
      toggle.prepend(icon);
    }
    icon.className = `bi bi-${item.dataset.icon} me-2`;
  } else if (icon) {
    icon.remove();
  }
  select.querySelectorAll(".dropdown-item").forEach((other) => {
    const chosen = other === item;
    other.setAttribute("aria-selected", chosen ? "true" : "false");
    other.querySelector(".uri-select-check").hidden = !chosen;
  });
  value.dispatchEvent(new Event("input", { bubbles: true }));
  value.dispatchEvent(new Event("change", { bubbles: true }));
  toggle.focus();
});

// what is wrong with the choice, said at once, as the form will say it when it is saved (the
// message of the form is there already, when the page is the answer to a save)
function note(select, wrong) {
  let text = select.parentElement.querySelector(".uri-select-note");
  const chosen = select.querySelector('.dropdown-item[aria-selected="true"]');
  if (!wrong) {
    if (text) text.remove();
    return;
  }
  const said = `${chosen.dataset.label} is only for ${chosen.dataset.onlyForNamed}`;
  if (select.parentElement.querySelector("p.invalid-feedback")?.textContent.trim() === said) return;
  if (!text) {
    text = document.createElement("div");
    text.className = "uri-select-note text-danger small mt-1";
    select.after(text);
  }
  text.textContent = said;
}

// the choices that are not for this resource are not in the menu (and one that was chosen
// before the type changed is marked: the form will not take it)
function onlyFor(root = document) {
  for (const select of root.querySelectorAll(".uri-select")) {
    const form = select.closest("form");
    const typed = form && form.querySelector('.uri-select-value[name="type"]');
    if (!typed || select.contains(typed)) continue;
    const kind = typed.value;
    let wrong = false;
    for (const item of select.querySelectorAll(".dropdown-item[data-only-for]")) {
      const allowed = JSON.parse(item.dataset.onlyFor).includes(kind);
      item.closest("li").hidden = !allowed;
      wrong ||= !allowed && item.getAttribute("aria-selected") === "true";
    }
    select.querySelector(".uri-select-toggle").classList.toggle("is-invalid", wrong);
    note(select, wrong);
  }
}

// The fields that only some resources have (data-only-for-fields of the tab: the name of
// the field and the classes of the resources that have it, as the shape says: the degree a
// thesis is in support of): the field is left out of the form of the others, and what is
// in it is not sent (the inputs are disabled; the form does not keep it either).
function fieldsOnlyFor(root = document) {
  for (const pane of root.querySelectorAll("[data-only-for-fields]")) {
    const typed = pane.querySelector('.uri-select-value[name="type"]');
    if (!typed) continue;
    for (const [name, classes] of Object.entries(JSON.parse(pane.dataset.onlyForFields))) {
      const field = pane.querySelector(`[name="${name}"]`);
      const item = field && field.closest(".mb-3");
      if (!item) continue;
      const hide = !classes.includes(typed.value);
      item.hidden = hide;
      item.querySelectorAll("input, select, textarea").forEach((each) => (each.disabled = hide));
    }
  }
}

const watch = () => {
  onlyFor();
  fieldsOnlyFor();
};
document.addEventListener("change", (event) => {
  if (event.target.matches && event.target.matches('.uri-select-value[name="type"]')) watch();
});
document.addEventListener("htmx:afterSettle", watch);
document.addEventListener("DOMContentLoaded", watch);
