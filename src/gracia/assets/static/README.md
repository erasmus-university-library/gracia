Served at `/_static/` (`config.add_static_view("_static", "gracia:static")`).

`css/bootstrap.min.css` and `scripts/bootstrap.bundle.min.js` are Bootstrap 5.3.2 (MIT), the same
files Deform ships for its widgets. `scripts/jobs.js` drives the jobs view.

`vendor/codemirror/` is CodeMirror 5.65.18 (MIT, `LICENSE` is in it): `codemirror.min.js` (the
npm package's `lib/codemirror.js`, minified once with `rjsmin`), `codemirror.css`, and the modes
`sparql.js`, `xml.js` (also XSLT) and `javascript.js` (also the JSON mode). They are served through
fanstatic like everything here (`gracia.static.editor_js`), and `scripts/editor.js` turns every
`<textarea data-editor="sparql">` , `"json"` or `"xml"` into an editor. To update it: fetch the
npm package `codemirror@5`, copy the five files, minify `lib/codemirror.js` again.
