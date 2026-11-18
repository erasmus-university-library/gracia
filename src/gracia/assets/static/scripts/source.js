// The source of a record: choosing another format in the list shows the text in that format (the
// button named "convert" does it; without this script it is the way to do it).
document.addEventListener("change", (event) => {
  const select = event.target.closest("select[data-source-format]");
  if (!select) return;
  const form = select.form;
  const convert = form.querySelector("button[name='convert']");
  if (convert) form.requestSubmit(convert);
});
