"""Generate a local mirror of the client site for agent development.

    python mocksite/build.py     # emit mocksite/site/
    python mocksite/serve.py     # serve it on :8080

Why a mirror instead of pointing the widget at the live site: phase 4 gives the
agent a navigate_to tool, and we need to watch it drive real navigation without
touching production. Two properties matter and both are easy to lose:

  * Real full page loads. Every path is its own HTML document, so navigate_to
    causes an actual browser navigation and the widget must survive it by
    rehydrating from sessionStorage -- exactly what it will face on WordPress.
    An SPA mock would hide that bug until launch.

  * Honest 404s. /pricing/ does not exist on the real site and must not exist
    here either. It is the single most likely page for the model to invent, so
    the mock has to punish the invention rather than absorb it.

Pages come from kb/raw/*.json, the same scrape that feeds the KB, so the mock
and the agent's world model cannot drift apart.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.config import get_settings  # noqa: E402

SETTINGS = get_settings()
OUT = Path(__file__).resolve().parent / "site"

# Nav is DERIVED from the scraped paths, never hand-written. Writing it by hand
# produced six links to pages that do not exist (/about-us, /web-development,
# /digital-marketing, /contact-us, /case-studies, /blog) -- the same invented
# URLs the agent is prone to. Anything absent from the scrape cannot appear.
NAV_ORDER = [
    ("/", "Home"),
    ("/about", "About"),
    ("/seo", "SEO"),
    ("/development", "Development"),
    ("/designing", "Design"),
    ("/marketing", "Marketing"),
    ("/advertisements", "Advertising"),
    ("/content-writing", "Content"),
    ("/case-studies", "Case Studies"),
    ("/blog", "Blog"),
    ("/contact", "Contact"),
]


def build_nav(available: set[str]) -> list[tuple[str, str]]:
    return [(p, label) for p, label in NAV_ORDER if p in available]

INLINE = [
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r'<a href="\2">\1</a>'),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"<em>\1</em>"),
]


def render_inline(text: str) -> str:
    out = html.escape(text, quote=False)
    # Unescape only the markdown link syntax we are about to convert.
    out = out.replace("&lt;", "&lt;")
    for pattern, repl in INLINE:
        out = pattern.sub(repl, out)
    return out


def markdown_to_html(md: str) -> str:
    """Minimal block renderer: headings, lists, paragraphs.

    Deliberately not a markdown library. The input is our own scrape output,
    not arbitrary user markdown, so six block types cover it.
    """
    lines = md.split("\n")
    out: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            close_list()
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_list()
            level = min(len(heading.group(1)) + 1, 6)  # h1 reserved for title
            out.append(f"<h{level}>{render_inline(heading.group(2))}</h{level}>")
            continue
        item = re.match(r"^[-*]\s+(.*)$", stripped)
        if item:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{render_inline(item.group(1))}</li>")
            continue
        close_list()
        out.append(f"<p>{render_inline(stripped)}</p>")

    close_list()
    return "\n".join(out)


STYLE = """
:root { --ink:#1a1d23; --muted:#5b6472; --line:#e3e7ee; --brand:#0b5fff; }
* { box-sizing:border-box; }
body { margin:0; font:16px/1.6 system-ui,-apple-system,Segoe UI,sans-serif;
       color:var(--ink); }
header { border-bottom:1px solid var(--line); padding:14px 24px;
         display:flex; gap:22px; align-items:center; flex-wrap:wrap; }
header strong { font-size:17px; }
header a { color:var(--muted); text-decoration:none; font-size:14px; }
header a:hover, header a[aria-current] { color:var(--brand); }
main { max-width:820px; margin:0 auto; padding:32px 24px 96px; }
h1 { font-size:30px; line-height:1.25; margin:0 0 6px; }
.path { color:var(--muted); font-size:13px; font-family:ui-monospace,monospace;
        margin-bottom:28px; }
h2,h3,h4,h5,h6 { line-height:1.3; margin:26px 0 8px; }
label { display:block; margin:14px 0 4px; font-weight:600; font-size:14px; }
input,textarea { width:100%; padding:9px 11px; border:1px solid var(--line);
                 border-radius:6px; font:inherit; }
button { margin-top:18px; background:var(--brand); color:#fff; border:0;
         padding:11px 20px; border-radius:6px; font:inherit; cursor:pointer; }
.note { background:#fff8e1; border-left:3px solid #f0b429; padding:10px 14px;
        margin:18px 0; font-size:14px; }
.ok { background:#e7f6ec; border-left:3px solid #1a7f37; padding:12px 14px;
      border-radius:4px; }
footer { border-top:1px solid var(--line); color:var(--muted); font-size:13px;
         padding:20px 24px; }
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<style>{style}</style>
</head>
<body>
<header><strong>Systematic IT Solutions</strong>{nav}</header>
<main>
<h1>{heading}</h1>
<div class="path">{path}</div>
{body}
</main>
<footer>Local development mirror &mdash; not the production site.</footer>
{widget}
</body>
</html>
"""

# The widget lands in phase 3. Until then the placeholder proves the injection
# point exists on every page, which is what makes cross-page rehydration
# testable at all.
WIDGET = '<script src="/widget.js" defer></script>'

# Gravity Forms populates fields from query params when a field is marked
# "allow dynamically populated". The agent will hand the visitor a prefilled
# URL rather than typing into the DOM, so the mock reproduces that contract:
# ?first_name=&email=&company=&message= land in the inputs.
CONTACT_FORM = """
<div class="note">Prefill contract: this form reads
<code>first_name</code>, <code>last_name</code>, <code>email</code>,
<code>phone</code>, <code>company</code>, <code>message</code> from the query
string, matching Gravity Forms dynamic population on the real site.</div>

<form id="contact" method="get" action="/contact/thank-you">
  <label for="first_name">First name</label>
  <input id="first_name" name="first_name" autocomplete="given-name" required>
  <label for="last_name">Last name</label>
  <input id="last_name" name="last_name" autocomplete="family-name">
  <label for="email">Email</label>
  <input id="email" name="email" type="email" autocomplete="email" required>
  <label for="phone">Phone</label>
  <input id="phone" name="phone" type="tel" autocomplete="tel">
  <label for="company">Company</label>
  <input id="company" name="company" autocomplete="organization">
  <label for="message">How can we help?</label>
  <textarea id="message" name="message" rows="5"></textarea>
  <button type="submit">Send enquiry</button>
</form>

<script>
(function () {
  var qs = new URLSearchParams(location.search);
  ['first_name','last_name','email','phone','company','message']
    .forEach(function (field) {
      var value = qs.get(field);
      if (!value) return;
      var el = document.getElementById(field);
      if (el) el.value = value;
    });
})();
</script>
"""


def nav_html(current: str, nav: list[tuple[str, str]]) -> str:
    links = []
    for path, label in nav:
        mark = ' aria-current="page"' if path == current else ""
        links.append(f'<a href="{path}"{mark}>{label}</a>')
    return "".join(links)


def clean_title(raw: str) -> str:
    return re.sub(r"\s*[-|]\s*Systematic IT Solutions\s*$", "", raw).strip() or "Home"


def write_page(
    path: str,
    title: str,
    description: str,
    body_html: str,
    nav: list[tuple[str, str]],
) -> Path:
    """One directory per path with an index.html -> real navigations."""
    rel = path.strip("/")
    target = OUT / rel / "index.html" if rel else OUT / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        PAGE.format(
            title=html.escape(f"{title} | Systematic IT Solutions"),
            description=html.escape(description or "", quote=True),
            style=STYLE,
            nav=nav_html(path, nav),
            heading=html.escape(title),
            path=html.escape(path),
            body=body_html,
            widget=WIDGET,
        ),
        encoding="utf-8",
    )
    return target


def main() -> int:
    raw_files = sorted(SETTINGS.raw_dir.glob("*.json"))
    if not raw_files:
        print("No kb/raw/*.json -- run kb/kb_build.py first.")
        return 1

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    pages = [json.loads(p.read_text(encoding="utf-8")) for p in raw_files]
    available = {p["path"].rstrip("/") or "/" for p in pages}
    nav = build_nav(available)

    written = 0
    contact_path = "/contact"
    for page in pages:
        title = clean_title(page["title"])
        body = markdown_to_html(page["markdown"])
        if (page["path"].rstrip("/") or "/") == contact_path:
            body += CONTACT_FORM
        write_page(page["path"], title, page["description"], body, nav)
        written += 1

    # Landing page for the prefilled-form submission, so the flow terminates
    # somewhere real instead of a 404.
    write_page(
        f"{contact_path}/thank-you",
        "Thank you",
        "Enquiry received.",
        '<div class="ok"><p>Thanks &mdash; your enquiry has been received. '
        "A specialist will be in touch shortly.</p></div>",
        nav,
    )
    written += 1

    # The widget is authored in widget/ and copied in, so the generated site
    # stays disposable: `rm -rf mocksite/site` must never lose real source.
    widget_src = Path(__file__).resolve().parent.parent / "widget" / "widget.js"
    if widget_src.exists():
        (OUT / "widget.js").write_text(
            widget_src.read_text(encoding="utf-8"), encoding="utf-8"
        )
    else:
        (OUT / "widget.js").write_text(
            "// widget/widget.js not found -- run from the project root.\n",
            encoding="utf-8",
        )

    print(f"  wrote {written} pages -> {OUT.relative_to(Path.cwd())}")
    print(f"  contact form prefills from query params")
    print(f"  /pricing/ intentionally absent (must 404)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
