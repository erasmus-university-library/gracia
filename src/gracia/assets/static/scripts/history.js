// The history of a record: View opens a dialog with the version, as text in the editor
// (read only) and, in the other tab, what it changed since the version before it. The data comes
// from the history page itself (`?txn=<number>`, JSON).
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-history-view]");
  if (!button) return;
  event.preventDefault();
  const dialog = document.getElementById("history-dialog");
  const problem = dialog.querySelector("[data-problem]");
  problem.classList.add("d-none");
  button.disabled = true;
  try {
    const response = await fetch(button.dataset.historyView, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`the version could not be read (${response.status})`);
    const revision = await response.json();
    dialog.querySelector("[data-title]").textContent = `Version ${revision.txn}`;
    dialog.querySelector("[data-when]").textContent = revision.when;
    const textarea = dialog.querySelector("textarea[data-editor]");
    textarea.value = revision.source;
    if (textarea.editor) textarea.editor.setValue(revision.source);
    const diff = dialog.querySelector("[data-diff]");
    diff.replaceChildren();
    if (revision.diff === null) {
      diff.textContent = "This is the first version: there is nothing before it.";
    } else if (revision.diff === "") {
      diff.textContent = "Nothing changed since the version before it.";
    } else {
      for (const line of revision.diff.split("\n")) {
        const row = document.createElement("div");
        row.textContent = line || " ";
        if (line.startsWith("+") && !line.startsWith("+++")) row.className = "text-success";
        else if (line.startsWith("-") && !line.startsWith("---")) row.className = "text-danger";
        else if (line.startsWith("@@")) row.className = "text-muted";
        diff.appendChild(row);
      }
    }
    bootstrap.Modal.getOrCreateInstance(dialog).show();
    const first = dialog.querySelector("[data-bs-target='#history-source']");
    if (first) bootstrap.Tab.getOrCreateInstance(first).show();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
});
