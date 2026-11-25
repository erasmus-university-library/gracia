// the calendar of a DateWidget: a menu that is made when it opens, with the months and
// the days named in the language of the repository (`data-locale`) and the week beginning
// on the day it begins there (`data-first-day`); the date that is clicked is put in the
// text in the order and with the separator of the repository
const pad = (n) => String(n).padStart(2, "0");

// what is typed, as numbers: {y, m, d}, with the parts that are not typed left out
function typed(text, order) {
  const parts = text.trim().split(/[-/.\s]+/);
  if (!parts.length || !parts.every((p) => /^\d+$/.test(p))) return {};
  let letters = parts[0].length === 4 ? "ymd" : order;
  if (parts.length === 2) letters = letters.replace("d", "");
  if (parts.length === 1) letters = "y";
  const by = {};
  [...letters].forEach((letter, i) => (by[letter] = Number(parts[i])));
  return by.y >= 1000 ? by : {};
}

const element = (tag, attributes = {}, text = "") => {
  const made = Object.assign(document.createElement(tag), attributes);
  made.textContent = text;
  return made;
};

function render(group, menu, year, month, selected) {
  const locale = group.dataset.locale;
  const first = Number(group.dataset.firstDay);
  const names = (options, date) =>
    new Intl.DateTimeFormat(locale, { ...options, timeZone: "UTC" }).format(date);
  const at = (y, m, d = 1) => new Date(Date.UTC(y, m, d));
  const go = (y, m) => () => {
    const moved = at(y, m);
    render(group, menu, moved.getUTCFullYear(), moved.getUTCMonth(), selected);
  };
  const button = (label, text, action) => {
    const made = element("button", { type: "button", className: "btn btn-sm btn-light" }, text);
    made.setAttribute("aria-label", label);
    made.addEventListener("click", action);
    return made;
  };

  const head = element("div", { className: "date-head d-flex align-items-center gap-1 mb-2" });
  head.append(
    button("-1 year", "\u00ab", go(year - 1, month)),
    button("-1 month", "\u2039", go(year, month - 1)),
    element("strong", { className: "flex-grow-1 text-center text-capitalize" },
      names({ month: "long", year: "numeric" }, at(year, month))),
    button("+1 month", "\u203a", go(year, month + 1)),
    button("+1 year", "\u00bb", go(year + 1, month)),
  );
  const grid = element("div", { className: "date-grid", role: "grid" });
  for (let i = 0; i < 7; i++) {  // 2024-01-07 was a Sunday
    grid.append(element("span", { className: "text-muted small text-center" },
      names({ weekday: "short" }, at(2024, 0, 7 + ((first + i) % 7)))));
  }
  const blanks = (at(year, month).getUTCDay() - first + 7) % 7;
  for (let i = 0; i < blanks; i++) grid.append(element("span"));
  const days = at(year, month + 1, 0).getUTCDate();
  const now = new Date();
  for (let d = 1; d <= days; d++) {
    const chosen = selected.y === year && selected.m === month + 1 && selected.d === d;
    const today = now.getFullYear() === year && now.getMonth() === month && now.getDate() === d;
    const day = element("button", {
      type: "button",
      className: "btn btn-sm " + (chosen ? "btn-primary" : today ? "btn-outline-primary" : "btn-light"),
    }, String(d));
    day.setAttribute("aria-label", names({ dateStyle: "long" }, at(year, month, d)));
    if (chosen) day.setAttribute("aria-pressed", "true");
    day.addEventListener("click", () => pick(group, menu, year, month + 1, d));
    grid.append(day);
  }
  menu.replaceChildren(head, grid);
}

function pick(group, menu, y, m, d) {
  const by = { y, m: pad(m), d: pad(d) };
  const text = group.querySelector("input:not(.date-native)");
  text.value = [...group.dataset.order].map((l) => by[l]).join(group.dataset.separator);
  text.dispatchEvent(new Event("input", { bubbles: true }));
  const toggle = group.querySelector(".date-pick");
  if (window.bootstrap) window.bootstrap.Dropdown.getOrCreateInstance(toggle).hide();
  text.focus();
}

// the calendar opens at the month of what is typed, else at this month
document.addEventListener("show.bs.dropdown", (event) => {
  const group = event.target.closest && event.target.closest(".date-input");
  const menu = group && group.querySelector(".date-calendar");
  if (!menu) return;
  const text = group.querySelector("input:not(.date-native)");
  const selected = typed(text.value, group.dataset.order);
  const now = new Date();
  const year = selected.y || now.getFullYear();
  const month = selected.y && selected.m ? selected.m - 1 : selected.y ? 0 : now.getMonth();
  render(group, menu, year, month, selected);
});
