// Code editors: a <textarea data-editor="sparql", "turtle", "json" or "xml" becomes a CodeMirror editor
// with syntax highlighting. The textarea stays in the form and CodeMirror keeps it in step (also
// when the form is submitted), so the server gets the same field as before.
(function () {
  const modes = {
    sparql: "application/sparql-query",
    turtle: "application/sparql-query", // close enough: keywords, IRIs, strings, variables
    json: { name: "javascript", json: true },
    xml: "application/xml", // also XSLT
  };

  function enhance(textarea) {
    const mode = modes[textarea.dataset.editor];
    if (!mode || textarea.editor) return;
    const editor = CodeMirror.fromTextArea(textarea, {
      mode,
      lineNumbers: true,
      lineWrapping: true,
      indentUnit: 2,
      viewportMargin: Infinity, // the editor grows with the text, the page scrolls
      readOnly: textarea.readOnly ? true : false,
    });
    editor.on("change", () => editor.save());
    textarea.editor = editor;
  }

  function enhanceAll(root) {
    root.querySelectorAll("textarea[data-editor]").forEach(enhance);
  }

  // an editor that was hidden (in a tab or a dialog) has no size: draw it when it is shown
  function refreshWithin(root) {
    root.querySelectorAll(".CodeMirror").forEach((element) => element.CodeMirror.refresh());
  }

  document.addEventListener("DOMContentLoaded", () => enhanceAll(document));
  document.addEventListener("shown.bs.tab", (event) => {
    const target = event.target.dataset.bsTarget;
    refreshWithin((target && document.querySelector(target)) || document);
  });
  document.addEventListener("shown.bs.modal", (event) => refreshWithin(event.target));
  document.addEventListener("htmx:afterSwap", (event) => enhanceAll(event.target)); // a part that was swapped in
})();
