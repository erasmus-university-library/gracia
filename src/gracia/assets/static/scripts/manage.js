// The manage page: the tab that is shown is in the address (?tab=name), so that a reload, or a link,
// comes back to it. Bootstrap does the switching.
document.addEventListener("shown.bs.tab", (event) => {
  const name = event.target.dataset.tab;
  if (!name) return;
  const url = new URL(location.href);
  url.searchParams.set("tab", name);
  url.searchParams.delete("saved");
  history.replaceState(null, "", url);
});

// A button that changes something and wants to be sure (`data-confirm` on its form).
document.addEventListener("submit", (event) => {
  const message = event.target.dataset && event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

// A dialog the server wants open (a form in it was not right): `data-open="true"`.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".modal[data-open='true']").forEach((element) => {
    bootstrap.Modal.getOrCreateInstance(element).show();
  });
});
