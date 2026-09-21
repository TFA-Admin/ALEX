# core/markdown_lite.py
"""
Just enough markdown to show COMMANDS.md on her page.

2026-09-21 (Craig: "I was hoping more for a reference either in the webui
or the controller"). No markdown library is installed and she must stay
offline, so this renders the subset the reference actually uses:
headings, tables, bullet lists, paragraphs, horizontal rules, **bold**,
`code`. Anything else comes through as escaped text, never as HTML. One
source: the file; this only draws it.
"""
import html
import re

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = _CODE.sub(r"<code>\1</code>", out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    return out


def _cells(line: str):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render(text: str) -> str:
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            level = max(1, min(level, 4))
            out.append(f"<h{level}>{_inline(stripped[level:].strip())}</h{level}>")
            i += 1
            continue

        if re.fullmatch(r"-{3,}", stripped):
            out.append("<hr/>")
            i += 1
            continue

        if stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            header = _cells(rows[0])
            body = rows[2:] if len(rows) > 1 and re.fullmatch(r"[\s|:-]+", rows[1].strip()) else rows[1:]
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in header)
                       + "</tr></thead><tbody>")
            for r in body:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _cells(r)) + "</tr>")
            out.append("</tbody></table>")
            continue

        if stripped.startswith("- "):
            out.append("<ul>")
            while i < len(lines) and lines[i].strip().startswith("- "):
                item = lines[i].strip()[2:]
                i += 1
                # a wrapped bullet continues on indented lines
                while i < len(lines) and lines[i].startswith("  ") and lines[i].strip() \
                        and not lines[i].strip().startswith("- "):
                    item += " " + lines[i].strip()
                    i += 1
                out.append(f"<li>{_inline(item)}</li>")
            out.append("</ul>")
            continue

        para = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt or nxt.startswith(("#", "|", "- ")) or re.fullmatch(r"-{3,}", nxt):
                break
            para.append(nxt)
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")

    return "\n".join(out)


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; padding: 24px 16px 48px; background: #0b0d12; color: #e4e6eb;
         font: 15px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }}
  main {{ max-width: 960px; margin: 0 auto; }}
  h1 {{ font-size: 24px; margin: 0 0 16px; }}
  h2 {{ font-size: 18px; margin: 32px 0 8px; color: #9fc3ff; }}
  h3 {{ font-size: 15px; margin: 20px 0 6px; }}
  p {{ margin: 8px 0; }}
  code {{ background: #1b1f2a; padding: 1px 5px; border-radius: 4px; font-size: 0.93em; }}
  table {{ border-collapse: collapse; width: 100%; margin: 10px 0 16px; font-size: 14px; }}
  th, td {{ border: 1px solid #2a2f3d; padding: 6px 8px; vertical-align: top; text-align: left; }}
  th {{ background: #141824; }}
  tr:nth-child(even) td {{ background: #0f121a; }}
  ul {{ margin: 6px 0 10px 22px; padding: 0; }}
  li {{ margin: 3px 0; }}
  hr {{ border: 0; border-top: 1px solid #2a2f3d; margin: 24px 0; }}
  strong {{ color: #ffffff; }}
</style></head>
<body><main>{body}</main></body></html>"""


def render_page(text: str, title: str = "A.L.E.X.") -> str:
    return _PAGE.format(title=html.escape(title), body=render(text))
