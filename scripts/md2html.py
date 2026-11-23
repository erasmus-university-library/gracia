"""Render a doctest markdown file as an HTML page.

    python scripts/md2html.py doctest.md doctest.html

The markdown is the doctest itself (`python -m doctest doctest.md` runs it): `#` and
`##` are headings, paragraphs are plain text, `code` spans and **bold** words are kept,
a block of lines indented by four spaces that starts with `>>>` is a doctest example
(its prompt is coloured, its expected result is grey), and a block that starts with `$`
is a shell session.
"""

import html
import re
import sys
from pathlib import Path


def inline(text: str) -> str:
    out, last = [], 0
    for m in re.finditer(r"`([^`\n]+)`|\*\*([^*\n]+)\*\*", text):
        if m.group(1):
            item = "<code>" + html.escape(m.group(1)) + "</code>"
        else:
            item = "<b>" + html.escape(m.group(2)) + "</b>"
        out.append(html.escape(text[last : m.start()]))
        out.append(item)
        last = m.end()
    out.append(html.escape(text[last:]))
    return "".join(out)


def is_example(line: str) -> bool:
    return line.startswith("    >>>") or line.startswith("    ...")


def example_block(lines: list[str]) -> str:
    out = []
    for line in lines:
        escaped = html.escape(line)
        m = re.match(r"(&gt;&gt;&gt;|\.\.\.)(.*)", escaped)
        if m:
            out.append(
                f'<span class="prompt">{m.group(1)}</span>'
                f'<span class="cont">{m.group(2)}</span>'
            )
        else:
            out.append(f'<span class="cont">{escaped}</span>')
    return "\n".join(out)


def render(lines: list[str]) -> str:
    out, j = [], 0
    while j < len(lines):
        line = lines[j]
        if is_example(line):
            code, result = [], []
            while j < len(lines) and (
                is_example(lines[j]) or lines[j].startswith("    ")
            ):
                if is_example(lines[j]):
                    code.append(lines[j][4:])
                elif lines[j].strip():
                    result.append(lines[j][4:])
                j += 1
            parts = ['<pre class="doctest">' + example_block(code) + "</pre>"]
            if result:
                parts.append(
                    '<pre class="result">' + html.escape("\n".join(result)) + "</pre>"
                )
            out.append("".join(parts))
            continue
        if line.startswith("    $"):
            block = []
            while j < len(lines) and (
                lines[j].startswith("    $") or (lines[j].startswith("    ") and block)
            ):
                if lines[j].startswith("    $"):
                    block.append("$ " + lines[j][5:].strip())
                elif lines[j].strip():
                    block.append(lines[j][4:])
                j += 1
            out.append('<pre class="shell">' + html.escape("\n".join(block)) + "</pre>")
            continue
        if line.startswith("## "):
            out.append("<h2>" + inline(line[3:].strip()) + "</h2>")
        elif line.startswith("# "):
            out.append("<h1>" + inline(line[2:].strip()) + "</h1>")
        elif line.strip():
            para = [line.strip()]
            while (
                j + 1 < len(lines)
                and lines[j + 1].strip()
                and not lines[j + 1].startswith((" ", "#"))
            ):
                j += 1
                para.append(lines[j].strip())
            out.append("<p>" + inline(" ".join(para)) + "</p>")
        j += 1
    title = next((x[2:].strip() for x in lines if x.startswith("# ")), "gracia")
    return "".join(
        [
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            "<title>" + html.escape(title) + "</title>",
            "<style>" + STYLE + "</style></head><body>",
            "\n".join(out),
            "</body></html>",
        ]
    )


STYLE = """
body { font: 15px/1.55 Georgia, serif; color: #1c1c1c;
       max-width: 46em; margin: 3em auto; padding: 0 1.5em; }
h1 { font-size: 1.45em; margin-bottom: .3em; }
h2 { font-size: 1.15em; margin-top: 2.4em; color: #333; }
p { margin: .9em 0; }
b { color: #2c4d76; }
pre { margin: .8em 0; padding: .5em .9em; border-radius: 4px;
      font: 13px/1.45 Menlo, Consolas, monospace; overflow-x: auto; }
.doctest { background: #f4f6f8; border-left: 3px solid #4a71a8; }
.prompt { color: #b03a44; }
.result { background: #f7f7f2; color: #565656; margin-top: -0.4em; }
.shell { background: #22272c; color: #dcdfe4; }
code { font: 13px Menlo, Consolas, monospace; background: #eef0f2; padding: 0 .15em; }
pre code { background: none; }
"""

if len(sys.argv) != 3:
    sys.exit(__doc__)
Path(sys.argv[2]).write_text(render(Path(sys.argv[1]).read_text().splitlines()))
