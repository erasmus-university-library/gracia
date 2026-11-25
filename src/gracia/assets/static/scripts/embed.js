// A page in a dialog (a form of a record in an iframe, opened by the button of a
// ReferenceWidget with `embed=1`): when the record has been saved the page has a
// #gracia-saved with the uri the record is pointed at by, how it is shown and where its
// form is; this tells the page around the dialog, which closes it and takes the uri and
// the label as its choice.
//
// The script is in the head of the page: it looks when the page has been read, or the
// element is not there yet.
(function () {
  function tell() {
    const saved = document.getElementById("gracia-saved");
    if (!saved || window.parent === window) return;
    window.parent.postMessage(
      {
        gracia: "saved",
        uri: saved.dataset.uri,
        label: saved.dataset.label,
        edit: saved.dataset.edit,
        icon: saved.dataset.icon,
      record: saved.dataset.record,
      },
      window.location.origin,
    );
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", tell);
  else tell();
})();
