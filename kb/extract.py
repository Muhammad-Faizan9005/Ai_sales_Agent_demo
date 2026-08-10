"""HTML -> markdown extraction for the knowledge base.

The target site is server-rendered WordPress/Elementor, so the pages arrive
complete from a plain GET -- no headless browser and no scraping API needed.
Uses stdlib html.parser rather than adding bs4/html2text for one job.

Elementor emits deeply nested divs and repeats the nav in a mobile drawer, so
the extractor drops chrome containers outright and keeps only heading, list and
paragraph structure. That structure is what makes the KB readable to the model
and small enough to fit the prompt.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Containers whose entire subtree is chrome, not content.
SKIP_TREES = {
    "script", "style", "noscript", "svg", "head", "nav", "header", "footer",
    "form", "button", "select", "textarea", "iframe", "template",
}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
BLOCKS = {"p", "div", "section", "article", "tr", "br", "hr"}


class Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._skip_tag: str | None = None
        self._heading: int | None = None
        # A heading's text can arrive as several data nodes (Elementor wraps
        # spans inside <h2>). The marker must be written once, not per node.
        self._heading_open = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in SKIP_TREES:
            self._skip_tag, self._skip_depth = tag, 1
            return
        # Elementor marks the mobile nav clone with these classes.
        classes = dict(attrs).get("class") or ""
        if any(m in classes for m in ("menu-toggle", "elementor-nav-menu", "screen-reader")):
            self._skip_tag, self._skip_depth = tag, 1
            return
        if tag in HEADINGS:
            self._heading = HEADINGS[tag]
            self._heading_open = False
            self.parts.append("\n\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in BLOCKS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if not self._skip_depth:
                    self._skip_tag = None
            return
        if tag in HEADINGS:
            self._heading = None
            self._heading_open = False
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip():
            return
        if self._heading:
            if self._heading_open:
                self.parts.append(f" {text.strip()}")
            else:
                self.parts.append(f"{'#' * self._heading} {text.strip()}")
                self._heading_open = True
        else:
            self.parts.append(text)


def html_to_markdown(html: str) -> str:
    parser = Extractor()
    parser.feed(html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # A heading immediately followed by the same text is an Elementor artifact.
    text = re.sub(r"(#+ ([^\n]+))\n+\2\n", r"\1\n", text)
    return text.strip()


def page_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def meta_description(html: str) -> str:
    for pattern in (
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)',
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']description["\']',
    ):
        match = re.search(pattern, html, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


def _self_check() -> None:
    html = """<html><head><title>Local SEO - Systematic</title>
    <meta name="description" content="Rank locally."></head><body>
    <nav><ul><li>Home</li><li>About</li></ul></nav>
    <h1>Local SEO</h1><p>We help you rank in map results.</p>
    <ul><li>GMB setup</li><li>Citations</li></ul>
    <script>var x = 'drop me';</script><footer>Copyright 2026</footer></body></html>"""
    md = html_to_markdown(html)
    assert page_title(html) == "Local SEO - Systematic", page_title(html)
    assert meta_description(html) == "Rank locally."
    assert "# Local SEO" in md
    assert "map results" in md
    assert "- GMB setup" in md
    assert "drop me" not in md, "script leaked"
    assert "Copyright" not in md, "footer leaked"
    assert "Home" not in md, "nav leaked"

    # Elementor splits heading text across spans; the marker must appear once.
    split = "<h2>Local SEO Service:<span> Targeted Strategies</span></h2>"
    out = html_to_markdown(split)
    assert out.count("#") == 2, f"heading marker repeated: {out!r}"
    assert out == "## Local SEO Service: Targeted Strategies", repr(out)

    print("extract.py self-check OK")


if __name__ == "__main__":
    _self_check()
