// the menu of a LanguageWidget: the language that is clicked is put in the code input
document.addEventListener("click", (event) => {
  const item = event.target.closest("[data-language]");
  const group = item && item.closest(".language-text");
  const input = group && group.querySelector("input.language-code");
  if (!input) return;
  input.value = item.dataset.language;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.focus();
});

// the caret is out of the tab order: the arrow down in the code opens the menu instead
document.addEventListener("keydown", (event) => {
  if (event.key !== "ArrowDown" || !event.target.matches("input.language-code")) return;
  const group = event.target.closest(".language-text");
  const toggle = group && group.querySelector("[data-bs-toggle=dropdown]");
  if (!toggle || !window.bootstrap) return;
  event.preventDefault();
  window.bootstrap.Dropdown.getOrCreateInstance(toggle).show();
  const first = group.querySelector(".dropdown-item");
  if (first) first.focus();
});
