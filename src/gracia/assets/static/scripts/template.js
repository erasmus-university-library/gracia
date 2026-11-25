// Two things follow the fields of a node as they are typed in. A field with
// data-template (gracia:propertyTemplate, as gracia.templates makes it) is made of the
// other fields of its node: it is filled in, unless one of the fields it needs is empty,
// as the server does when the record is saved. An element in the bar of a node with
// data-header-template (gracia:headerTemplate, gracia:headerAddOnTemplate) gets the
// text of the template (without the separators , ; : left at its ends), or its data-fallback when that is empty, and is hidden then if
// that is empty too. A field can have a template for each value of another field (the
// select of the kind of identifier, say): the one that fits is used, and when none does
// (or it makes nothing) what was made before is taken away again. A template is a list of pieces: ["text", s], ["source", name] and
// ["optional", [pieces]], which is left out when one of the sources inside it is empty.
(function () {
  const holder = (element) => {
    const item = element.closest('[class*="item-"]');
    return item && item.parentElement;
  };

  const render = (pieces, values) => {
    let out = "";
    for (const [kind, value] of pieces) {
      if (kind === "text") {
        out += value;
      } else if (kind === "source") {
        const text = (values[value] || "").trim();
        if (!text) return null;
        out += text;
      } else {
        out += render(value, values) || "";
      }
    }
    return out;
  };

  const sources = (pieces) =>
    pieces.flatMap(([kind, value]) =>
      kind === "source" ? [value] : kind === "optional" ? sources(value) : []
    );

  document.addEventListener("input", (event) => {
    const container = holder(event.target);
    if (!container) return;
    const fields = {};
    for (const field of container.querySelectorAll("[name]")) {
      if (holder(field) === container) fields[field.name] = field;
    }
    const valuesOf = (pieces) => {
      const values = {};
      for (const name of sources(pieces)) {
        values[name] = fields[name] ? fields[name].value : "";
      }
      return values;
    };
    // the bar of a node (the text and the badge in it, gracia:headerTemplate)
    const item = container.closest("details");
    for (const part of item ? item.querySelectorAll("summary [data-header-template]") : []) {
      const pieces = JSON.parse(part.dataset.headerTemplate);
      const made = (render(pieces, valuesOf(pieces)) || "").replace(
        /^[\s,;:]+|[\s,;:]+$/g,
        ""
      );
      part.textContent = made || part.dataset.fallback || "";
      part.hidden = !part.textContent;
    }
    for (const target of container.querySelectorAll("[data-template]")) {
      if (holder(target) !== container || target === event.target) continue;
      const variants = JSON.parse(target.dataset.template);
      const names = variants.flatMap((v) => [...sources(v.pieces), ...(v.when ? [v.when[0]] : [])]);
      if (!names.includes(event.target.name)) continue;
      const made = madeOf(variants, fields);
      if (made) {
        target.value = made;
        target.dataset.made = made;
      } else if (target.value === target.dataset.made) {
        target.value = ""; // what was made for what is not there now (or another value)
        target.dataset.made = "";
      }
    }
  });

  // the text that the template for what the fields have now makes: the one for the value
  // of the property that it is for, else the one for all
  const madeOf = (variants, fields) => {
    const variant = variants.find(
      (v) => !v.when || (fields[v.when[0]] && fields[v.when[0]].value === v.when[1])
    );
    if (!variant) return "";
    const values = {};
    for (const name of sources(variant.pieces)) {
      values[name] = fields[name] ? fields[name].value : "";
    }
    return (render(variant.pieces, values) || "").trim();
  };

  // a field that has what its template makes: when the template changes (the other value
  // of the property that it depends on), it is that, and not what a person typed, that
  // is taken away
  const remember = (root) => {
    for (const target of root.querySelectorAll("[data-template]")) {
      const container = holder(target);
      const fields = {};
      for (const field of container ? container.querySelectorAll("[name]") : []) {
        if (holder(field) === container) fields[field.name] = field;
      }
      const made = madeOf(JSON.parse(target.dataset.template), fields);
      if (made && made === target.value) target.dataset.made = made;
    }
  };
  document.addEventListener("DOMContentLoaded", () => remember(document));
  document.addEventListener("htmx:afterSwap", (event) => remember(event.target));
})();
