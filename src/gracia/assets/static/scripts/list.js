// The rows of a ListWidget (an rdf:list) are moved by their handle: dragged to another
// place, or with the arrow keys when the handle has the focus. The order of the rows in the
// page is the order that is posted. When a row has been moved the list asks the server to
// show the rows again (the event `reorder`, which the list has for htmx), so that the +
// is at the last row and the x at the others; the handle of the row that moved then has
// the focus again.
let dragged = null;
let refocus = null;  // {oid, index} of the handle that has the focus when the rows come

const rowOf = (node) => node.closest && node.closest(".list > .bag-items > .bag-item");

function reordered(row) {
  const list = row.closest(".list");
  refocus = { oid: list.id, index: [...list.querySelectorAll(".bag-item")].indexOf(row) };
  list.dispatchEvent(new CustomEvent("reorder"));
}

// the row can be dragged only while the handle is held: the inputs in it keep their text
// selection
document.addEventListener("pointerdown", (event) => {
  const handle = event.target.closest && event.target.closest(".list-handle");
  const row = handle && rowOf(handle);
  if (row) row.draggable = true;
});

document.addEventListener("dragstart", (event) => {
  const row = event.target.closest && event.target.closest(".list .bag-item[draggable=true]");
  if (!row) return;
  dragged = row;
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", "");
  row.classList.add("dragging");
});

document.addEventListener("dragover", (event) => {
  const over = dragged && rowOf(event.target);
  if (!over || over.parentNode !== dragged.parentNode) return;
  event.preventDefault();
  if (over === dragged) return;
  const box = over.getBoundingClientRect();
  const after = event.clientY > box.top + box.height / 2;
  over.parentNode.insertBefore(dragged, after ? over.nextSibling : over);
});

document.addEventListener("drop", (event) => {
  if (dragged) event.preventDefault();
});

document.addEventListener("dragend", () => {
  if (!dragged) return;
  const row = dragged;
  dragged = null;
  row.classList.remove("dragging");
  row.draggable = false;
  reordered(row);
});

document.addEventListener("pointerup", () => {
  document.querySelectorAll(".list .bag-item[draggable=true]").forEach((row) => {
    if (!dragged) row.draggable = false;  // held, but not dragged
  });
});

document.addEventListener("keydown", (event) => {
  const handle = event.target.closest && event.target.closest(".list-handle");
  const row = handle && rowOf(handle);
  if (!row || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
  event.preventDefault();
  const other = event.key === "ArrowUp" ? row.previousElementSibling : row.nextElementSibling;
  if (!other) return;
  row.parentNode.insertBefore(row, event.key === "ArrowUp" ? other : other.nextSibling);
  handle.focus();
  reordered(row);
});

document.addEventListener("htmx:afterSettle", () => {
  if (!refocus) return;
  const list = document.getElementById(refocus.oid);
  const handle = list && list.querySelectorAll(".list-handle")[refocus.index];
  refocus = null;
  if (handle) handle.focus();
});
