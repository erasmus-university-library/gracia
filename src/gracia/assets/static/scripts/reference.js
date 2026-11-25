// A ReferenceWidget: a reference to another resource.
//
// Typeahead: what is typed in the text is looked for in the lookup of the repository
// (`data-lookup`), among the resources of the rdf types in `data-classes`; the record that
// is chosen puts its uri in the hidden input (what is posted) and its label in the text.
// What is typed after a choice takes the choice away: the uri that is kept is always the
// one of the label that is shown. A click in the text opens the menu too, filtered on the
// text that is there (the label of a choice, or what was typed), and with every record
// there is when it is empty (a few candidates are then to be seen at once); when there
// are more than a page of them the menu has a pager.
//
// A reference can follow a field next to it in its node (data-follow: the field, and for
// each of its values the classes, the types to add and the icon: the role of a contributor
// says whether it is a person or an organization). When that field changes the lookup and the
// New button follow it, and a choice that was for another kind is taken away.
//
// The button at the end of the group is Edit when a resource is chosen and New when none
// is. Either opens the form of the record in a dialog (an iframe, with `embed=1`: the form
// without the page around it). When the record is saved the form says so (embed.js) and
// the dialog closes: what was minted or changed, the uri and the label, is the choice.
(function () {
  const DELAY = 200; // ms after the last key
  const CLOSE_DELAY = 1000; // ms the dialog stays after the save: the "Saved." is seen
  const state = new WeakMap(); // reference -> {timer, abort, items, active, mode}

  const parts = (reference) => ({
    value: reference.querySelector(".reference-value"),
    input: reference.querySelector(".reference-input"),
    list: reference.querySelector(".reference-list"),
    button: reference.querySelector(".reference-open"),
  });

  const info = (reference) => {
    if (!state.has(reference))
      state.set(reference, { timer: null, abort: null, items: [], active: -1, mode: "found", paging: null });
    return state.get(reference);
  };

  const news = (reference) => JSON.parse(reference.dataset.new || "[]");

  // the icon at the start of the group is the one of the type of the record that is chosen;
  // when none is, the one of the type it can be (or a link, if it can be of several)
  function setIcon(reference, name) {
    const icon = reference.querySelector(".reference-icon");
    if (!icon) return;
    icon.firstElementChild.className = `bi bi-${name || icon.dataset.defaultIcon}`;
  }

  // the button says what it does: Edit a resource that is chosen, else New
  function refresh(reference) {
    const { value, button } = parts(reference);
    if (!button) return;
    const chosen = value.value !== "";
    button.querySelector(".reference-open-text").textContent = chosen ? "Edit" : "New";
    button.firstElementChild.className = `bi bi-${chosen ? "pencil" : "stars"} me-1`;
    button.hidden = chosen ? !reference.dataset.editUrl : news(reference).length === 0;
  }

  function close(reference) {
    const { input, list } = parts(reference);
    list.classList.remove("show");
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    info(reference).active = -1;
  }

  function activate(reference, index) {
    const { input, list } = parts(reference);
    const options = list.querySelectorAll("[role=option]");
    if (!options.length) return;
    const at = (index + options.length) % options.length;
    options.forEach((option, i) => option.classList.toggle("active", i === at));
    input.setAttribute("aria-activedescendant", options[at].id);
    options[at].scrollIntoView({ block: "nearest" });
    info(reference).active = at;
  }

  // the menu: what was found ("found"), or the types of record a new one can be ("new")
  function show(reference, items, message, mode = "found", paging = null) {
    const { input, list } = parts(reference);
    list.replaceChildren();
    info(reference).items = items;
    info(reference).mode = mode;
    info(reference).paging = paging;
    items.forEach((found, i) => {
      const option = document.createElement("li");
      option.id = `${list.id}-${i}`;
      option.setAttribute("role", "option");
      option.className = "dropdown-item d-flex align-items-baseline gap-2";
      option.append(found.label);
      if (found.kind) {  // what the resource is: its type, as the shape names it
        const kind = document.createElement("small");
        kind.className = "text-muted";
        kind.textContent = found.kind;
        option.append(kind);
      }
      // the one that is the choice now: a dark pill (the grey row is only where the keys are)
      if (mode === "found" && found.uri && found.uri === parts(reference).value.value) {
        const chosen = document.createElement("span");
        chosen.className = "badge rounded-pill text-bg-secondary fw-normal";
        chosen.textContent = "selected";
        option.append(chosen);
        option.setAttribute("aria-selected", "true");
      }
      // the record is told when the mouse is over the row (its number can be typed)
      if (found.record) option.title = found.record;
      // mousedown, not click: the input has not lost the focus yet
      option.addEventListener("mousedown", (event) => {
        event.preventDefault();
        choose(reference, i);
      });
      list.append(option);
    });
    if (!items.length) {
      const none = document.createElement("li");
      none.className = "dropdown-item disabled";
      none.textContent = message || "Nothing found";
      list.append(none);
    }
    if (paging && paging.pages > 1) list.append(pager(reference, paging));
    list.classList.add("show");
    input.setAttribute("aria-expanded", "true");
    info(reference).active = -1;
    // the keys start at the choice there is, else at the first
    const current = mode === "found" ? items.findIndex((found) => found.uri === parts(reference).value.value) : -1;
    if (items.length) activate(reference, Math.max(current, 0));
  }

  // the pager at the bottom of the menu: the page before, where we are, the page after. Its
  // buttons keep the focus in the text (mousedown, and out of the tab order): the keys
  // PageUp and PageDown in the text do the same.
  function pager(reference, paging) {
    const row = document.createElement("li");
    row.className = "reference-pager";
    row.setAttribute("role", "presentation");
    const go = (label, text, page) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-sm btn-light";
      button.tabIndex = -1;
      button.textContent = text;
      button.setAttribute("aria-label", label);
      button.disabled = page < 1 || page > paging.pages;
      button.addEventListener("mousedown", (event) => {
        event.preventDefault();
        if (!button.disabled) search(reference, page);
      });
      return button;
    };
    const first = (paging.page - 1) * paging.size + 1;
    const last = Math.min(paging.page * paging.size, paging.total);
    const where = document.createElement("span");
    where.className = "small text-muted";
    where.textContent = `${first}\u2013${last} of ${paging.total}${paging.truncated ? "+" : ""}`;
    where.setAttribute("aria-live", "polite");
    row.append(go("Previous page", "\u2039", paging.page - 1), where, go("Next page", "\u203a", paging.page + 1));
    return row;
  }

  // what is shown of the choice is what the bar of a node says of it (data-label, template.js)
  function take(reference, found) {
    const { value, input } = parts(reference);
    value.value = found.uri;
    value.dataset.label = found.label;
    input.value = found.label;
    input.title = found.record || found.uri; // the record the resource is in
    input.classList.remove("reference-unset");
    reference.classList.add("is-set");
    reference.dataset.editUrl = found.edit || "";
    setIcon(reference, found.icon);
    refresh(reference);
    value.dispatchEvent(new Event("input", { bubbles: true }));
    value.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // ---- following a field of the node: what it points at is what the value there says
  function applyFollow(reference, fromUser = false) {
    if (!reference.dataset.follow) return;
    const follow = JSON.parse(reference.dataset.follow);
    const scope = reference.closest(".accordion-body") || reference.closest("form");
    const field = scope && scope.querySelector(`[name="${follow.name}"]`);
    const icon = reference.querySelector(".reference-icon");
    if (reference.dataset.firstClasses === undefined) { // what it was before there was a value
      reference.dataset.firstClasses = reference.dataset.classes;
      reference.dataset.firstNew = reference.dataset.new;
      if (icon) icon.dataset.firstIcon = icon.dataset.defaultIcon;
    }
    const entry = field ? follow.map[field.value] : null;
    const classes = entry ? JSON.stringify(entry.classes) : reference.dataset.firstClasses;
    const other = classes !== reference.dataset.classes;
    reference.dataset.classes = classes;
    reference.dataset.new = entry ? JSON.stringify(entry.new) : reference.dataset.firstNew;
    if (icon) icon.dataset.defaultIcon = entry ? entry.icon : icon.dataset.firstIcon;
    const { value, input } = parts(reference);
    if (fromUser && other && value.value) { // a choice for another kind of record is not a choice
      value.value = "";
      delete value.dataset.label;
      input.value = "";
      input.removeAttribute("title");
      reference.classList.remove("is-set");
      reference.dataset.editUrl = "";
      value.dispatchEvent(new Event("input", { bubbles: true }));
    }
    if (!value.value) setIcon(reference, "");
    refresh(reference);
  }

  document.addEventListener("change", (event) => {
    const name = event.target.name;
    const scope = name && event.target.closest(".accordion-body");
    if (!scope) return;
    scope.querySelectorAll(".reference[data-follow]").forEach((reference) => {
      if (JSON.parse(reference.dataset.follow).name === name) applyFollow(reference, true);
    });
  });

  function choose(reference, index) {
    const found = info(reference).items[index];
    if (!found) return;
    close(reference);
    if (info(reference).mode === "new") return openDialog(reference, found.url, found.label);  // "New person"
    take(reference, found);
  }

  // Looks for the text, and shows a page of what is found: what was typed, or the label
  // of a choice. With nothing there it is every record of the classes.
  async function search(reference, page = 1) {
    const { input } = parts(reference);
    const text = input.value.trim();
    const mine = info(reference);
    if (mine.abort) mine.abort.abort();
    const query = new URLSearchParams({ q: text, page: String(page) });
    JSON.parse(reference.dataset.classes || "[]").forEach((c) => query.append("type", c));
    mine.abort = new AbortController();
    try {
      const response = await fetch(`${reference.dataset.lookup}?${query}`, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
        signal: mine.abort.signal,
      });
      const body = await response.json();
      if (!response.ok) return show(reference, [], "Search is not available");
      show(reference, body.items, "", "found", body);
    } catch (error) {
      if (error.name !== "AbortError") show(reference, [], "Search is not available");
    }
  }

  // ---- the dialog: one for the page, made when it is first needed
  //
  // The form in a dialog is in an iframe, and may have a dialog of its own (a work, its journal,
  // the publisher of the journal): the backdrop of the one on top covers its own page, the
  // iframe, and not the header of the dialog it is in. So it says to the page around it that
  // it is open, and that page dims the header of its dialog (the class `covered`).
  const tellParent = (open) => {
    if (window.parent !== window) window.parent.postMessage({ gracia: "dialog", open }, window.location.origin);
  };
  let dialog = null;
  let editing = null; // the reference the dialog is open for
  let closer = null; // the timer that closes it after a save

  function ensureDialog() {
    if (dialog) return dialog;
    const element = document.createElement("div");
    element.className = "modal fade";
    element.id = "reference-dialog";
    element.tabIndex = -1;
    element.setAttribute("aria-labelledby", "reference-dialog-title");
    element.setAttribute("aria-hidden", "true");
    element.innerHTML =
      '<div class="modal-dialog modal-xl"><div class="modal-content">' +
      '<div class="modal-header"><h2 class="modal-title h5" id="reference-dialog-title"></h2>' +
      '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button></div>' +
      '<div class="modal-body p-0"><iframe class="reference-frame" title="Form"></iframe></div>' +
      "</div></div>";
    document.body.append(element);
    // a dialog that is still opening cannot be closed: it is closed when it has opened
    element.addEventListener("shown.bs.modal", () => {
      if (dialog.closing) dialog.modal.hide();
    });
    element.querySelector("iframe").addEventListener("load", () => element.classList.remove("covered"));
    element.addEventListener("hidden.bs.modal", () => {
      clearTimeout(closer);
      dialog.closing = false;
      element.classList.remove("covered");
      tellParent(false);
      element.querySelector("iframe").src = "about:blank"; // nothing of the form stays
      const reference = editing;
      editing = null;
      if (reference) parts(reference).button.focus();
    });
    dialog = { element, modal: window.bootstrap.Modal.getOrCreateInstance(element), closing: false };
    return dialog;
  }

  function openDialog(reference, url, title) {
    if (!url || !window.bootstrap) return;
    const { element, modal } = ensureDialog();
    editing = reference;
    clearTimeout(closer);
    dialog.closing = false;
    tellParent(true);
    element.querySelector("#reference-dialog-title").textContent = title;
    element.querySelector("iframe").src = url;
    modal.show();
  }

  // the form in the dialog says that it saved the record (embed.js)
  window.addEventListener("message", (event) => {
    const frame = dialog && dialog.element.querySelector("iframe");
    if (!frame || event.origin !== window.location.origin || event.source !== frame.contentWindow) return;
    const said = event.data;
    if (said && said.gracia === "dialog") { // the form has a dialog open, or has not any more
      dialog.element.classList.toggle("covered", !!said.open);
      return;
    }
    if (!said || said.gracia !== "saved" || !editing) return;
    // the field has what was saved at once; the dialog goes a moment later
    take(editing, {
      uri: said.uri,
      label: said.label,
      edit: said.edit,
      icon: said.icon,
      record: said.record,
    });
    clearTimeout(closer);
    closer = setTimeout(() => {
      dialog.closing = true;
      dialog.modal.hide();
    }, CLOSE_DELAY);
  });

  // ---- what the person does
  document.addEventListener("click", (event) => {
    const button = event.target.closest && event.target.closest(".reference-open");
    const reference = button && button.closest(".reference");
    if (!reference) return;
    const { value, input } = parts(reference);
    if (value.value) return openDialog(reference, reference.dataset.editUrl, `Edit ${input.value}`);
    const kinds = news(reference);
    if (kinds.length === 1) return openDialog(reference, kinds[0].url, `New ${kinds[0].label.toLowerCase()}`);
    show(reference, kinds.map((kind) => ({ label: `New ${kind.label.toLowerCase()}`, url: kind.url })), "", "new");
  });

  document.addEventListener("input", (event) => {
    const input = event.target.closest && event.target.closest(".reference-input");
    const reference = input && input.closest(".reference");
    if (!reference) return;
    const { value } = parts(reference);
    value.value = ""; // what is typed is not the choice any more
    delete value.dataset.label;
    reference.classList.remove("is-set");
    reference.dataset.editUrl = "";
    setIcon(reference, "");
    refresh(reference);
    input.removeAttribute("title");
    input.classList.toggle("reference-unset", input.value.trim() !== "");
    clearTimeout(info(reference).timer);
    info(reference).timer = setTimeout(() => search(reference), DELAY);
  });

  // a click in the text opens the menu (not when it is open already)
  document.addEventListener("click", (event) => {
    const input = event.target.closest && event.target.closest(".reference-input");
    const reference = input && input.closest(".reference");
    if (reference && !parts(reference).list.classList.contains("show")) search(reference);
  });

  document.addEventListener("keydown", (event) => {
    const input = event.target.closest && event.target.closest(".reference-input");
    const reference = input && input.closest(".reference");
    if (!reference) return;
    const mine = info(reference);
    const open = parts(reference).list.classList.contains("show");
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!open) return search(reference);
      event.preventDefault();
      activate(reference, mine.active + (event.key === "ArrowDown" ? 1 : -1));
    } else if ((event.key === "PageDown" || event.key === "PageUp") && open && mine.paging) {
      event.preventDefault();
      const to = mine.paging.page + (event.key === "PageDown" ? 1 : -1);
      if (to >= 1 && to <= mine.paging.pages) search(reference, to);
    } else if (event.key === "Enter" && open) {
      event.preventDefault(); // not the submit of the form
      if (mine.active >= 0) choose(reference, mine.active);
    } else if (event.key === "Escape" && open) {
      event.preventDefault();
      close(reference);
    }
  });

  document.addEventListener("focusout", (event) => {
    const reference = event.target.closest && event.target.closest(".reference");
    if (reference) setTimeout(() => !reference.contains(document.activeElement) && close(reference), 100);
  });

  const start = () =>
    document.querySelectorAll(".reference").forEach((reference) => {
      applyFollow(reference);
      refresh(reference);
    });
  document.addEventListener("htmx:afterSettle", start);
  document.addEventListener("DOMContentLoaded", start);
})();
