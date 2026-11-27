"""The citations of the docs: a CSL JSON item rendered in a style.

Rendering is the expensive part, so it is done once when a doc is made (the
`Citations` extension) and kept in the doc as `citation`; nothing renders on the fly.
"""

import functools
import json
import logging
import re
import threading
from importlib import resources

from citeproc import (
    Citation,
    CitationItem,
    CitationStylesBibliography,
    CitationStylesStyle,
    formatter,
)
from citeproc.source.json import CiteProcJSON

DEFAULT_STYLE = "apa"
STYLE_DIR = "csl"
# the number a numeric style puts before an entry: `[1]`, `(1)`, `1.`; one record's
# citation has no place in a list, so it has no number
_NUMBER = re.compile(r"^(?:\[\d+\]|\(\d+\)|\d+\.)\s*")


log = logging.getLogger("gracia.citations")

_lock = threading.Lock()  # a style keeps state while it renders


def style_names() -> list[str]:
    """The styles that ship in the assets dir (`assets/csl/<name>.csl`)."""
    folder = resources.files("gracia") / "assets" / STYLE_DIR
    return sorted(p.name[:-4] for p in folder.iterdir() if p.name.endswith(".csl"))


@functools.lru_cache(maxsize=8)
def _style(name: str) -> CitationStylesStyle:
    path = resources.files("gracia") / "assets" / STYLE_DIR / f"{name}.csl"
    with resources.as_file(path) as real:
        return CitationStylesStyle(str(real), validate=False)


@functools.lru_cache(maxsize=32)
def is_numeric(name: str) -> bool:
    path = resources.files("gracia") / "assets" / STYLE_DIR / f"{name}.csl"
    return "citation-number" in path.read_text(encoding="utf-8")


def render(name: str, blobs: dict[str, str]) -> dict[str, str]:
    """The citation (HTML) of each CSL JSON item, by key.

    Each item is rendered on its own: a bibliography of them all would number, sort and
    add year suffixes (2020a) to what is one record's citation. An item that cannot be
    rendered is left out (and logged).
    """
    out: dict[str, str] = {}
    with _lock:
        style = _style(name)
        for key, blob in blobs.items():
            try:
                item = json.loads(blob)
                source = CiteProcJSON([item])
                bibliography = CitationStylesBibliography(style, source, formatter.html)
                bibliography.register(Citation([CitationItem(item["id"])]))
                text = str(bibliography.bibliography()[0])
                out[key] = _NUMBER.sub("", text) if is_numeric(name) else text
            except Exception as err:  # the library raises whatever the item makes it
                log.warning("no citation for %s: %s", key, err)
    return out
