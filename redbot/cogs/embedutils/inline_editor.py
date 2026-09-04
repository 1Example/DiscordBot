"""Turn ``editor.html`` into a fragment that lives inside a dashboard page.

``editor.html`` is Glitchii's embed builder, shipped as a complete HTML
document: ~91KB of its own CSS written against ``html``/``body``, five inline
scripts, and a handful of CDN dependencies. Served as-is it can only be a
standalone page, which is why the editor used to open outside the dashboard
shell entirely.

This module rewrites it, at import time, into something that can be dropped
into a normal module page next to the topbar, guild hero and breadcrumb:

* every CSS rule is scoped to ``#eu-editor``, with ``html`` / ``body`` /
  ``:root`` selectors rewritten to the container itself, so the editor's
  ``body.gui`` / ``body.no-preview`` mode switching keeps working;
* ``<body class="gui emptyEmbed">`` becomes ``<div id="eu-editor" class="gui
  emptyEmbed">``, and the scripts' ``document.body`` references are pointed at
  it, so the same mode switching happens from JavaScript too;
* the editor's ``{% include "includes/scripts.html" %}`` is dropped, because
  the host page has already loaded jQuery, Bootstrap and the rest, and loading
  them a second time would trample the page they are running on.

The upstream file is not modified, so it can still be replaced wholesale from
the original project. Everything here is derived from it at runtime.
"""

from __future__ import annotations

import os
import re
import typing as t

__all__ = ("ROOT_ID", "build_fragment")

ROOT_ID = "eu-editor"
ROOT_SELECTOR = f"#{ROOT_ID}"

_EDITOR_PATH = os.path.join(os.path.dirname(__file__), "editor.html")

# At-rules whose body is a list of style rules, so scoping has to recurse into
# them. Anything not listed (@keyframes, @font-face, @import, @page, ...) has a
# body that is not selectors and is copied through untouched.
_NESTED_AT_RULES = (
    "@media",
    "@supports",
    "@document",
    "@layer",
    "@container",
    "@scope",
)

# Appended after the scoped stylesheet. These are the four things that only
# break once the document becomes an element, and they are kept here rather
# than special-cased inside the scoper so it stays a plain mechanical rewrite.
_INLINE_ADJUSTMENTS = f"""
/* The editor's layout is built on `height: 100%` all the way down from `body`.
   In a page it has no viewport to resolve against, so the container is given a
   concrete workspace height instead. `position: relative` matters just as
   much: `.main` is absolutely positioned, and without a positioned ancestor it
   would escape the editor and lay itself out against the page. */
{ROOT_SELECTOR} {{
  position: relative;
  height: min(1040px, calc(100vh - 250px));
  min-height: 600px;
  border-radius: 12px;
  overflow: hidden;
}}

/* `.notification` and `.done` were fixed to the viewport, which was right for a
   full-screen document and wrong here: they floated over the dashboard instead
   of over the editor. Anchored to the container, they land where they did. */
{ROOT_SELECTOR} .notification,
{ROOT_SELECTOR} .done {{
  position: absolute;
}}

/* The dashboard's own controls stop at the editor's edge. Its inputs and
   buttons are styled by the stylesheet above, and the shell's form rules would
   otherwise repaint them mid-layout. */
{ROOT_SELECTOR} input,
{ROOT_SELECTOR} select,
{ROOT_SELECTOR} textarea,
{ROOT_SELECTOR} button {{
  font-family: inherit;
}}

/* ---- palette -----------------------------------------------------------
   The editor was built to fill a window, so its chrome is painted in
   Discord's greys (#36393f, #2f3136, #292b2f, #212226). Dropped into a
   translucent blue card those read as a grey slab bolted onto the page, so
   the chrome is moved onto the shell's surfaces.

   The embed preview is deliberately NOT retinted. Its whole job is to show
   what the embed will look like once it is posted, so it keeps Discord's
   background, text colours and code-block styling. Retinting it would make
   it a prettier preview of something that does not exist.

   Values come from the dashboard's tokens with the literal as a fallback, so
   this tracks the theme without depending on it. */
{ROOT_SELECTOR} {{
  --fullEmbedBackground: transparent;
  --side1Background: transparent;
  --background-tertiary: rgba(255, 255, 255, .08);
  background: transparent;
}}

/* Both panes sit directly on the card. */
{ROOT_SELECTOR} .main .side1,
{ROOT_SELECTOR} .main .side2 {{
  background-color: transparent;
}}
{ROOT_SELECTOR} .main .side1 {{
  border-right: 1px solid var(--cx-panel-2-bd, rgba(120, 160, 255, .10));
}}

/* The accordion rows, the JSON pane and the bottom bar: the editor's own
   panels, so they take the shell's nested-panel surface. */
{ROOT_SELECTOR} .top,
{ROOT_SELECTOR} .side1 .item.top,
{ROOT_SELECTOR} .top > .gui .item,
{ROOT_SELECTOR} .top > .gui .item ~ .edit,
{ROOT_SELECTOR} .side1 .bottom .box,
{ROOT_SELECTOR} .bottom .colors,
{ROOT_SELECTOR} .bottom .sending {{
  background-color: var(--cx-panel-2, rgba(90, 130, 220, .07));
  border-color: var(--cx-panel-2-bd, rgba(120, 160, 255, .10));
}}
{ROOT_SELECTOR} .top > .gui .item:not(.inlineField):not(.guiEmbedName).active {{
  background-color: var(--cx-panel-3, rgba(255, 255, 255, .045));
}}
{ROOT_SELECTOR} .chooser {{
  background-color: transparent;
}}

/* `.embed` drew its default accent bar with --fullEmbedBackground, which is
   now transparent; the picked colour still overrides this inline. */
{ROOT_SELECTOR} .embed {{
  border-left-color: rgba(130, 175, 255, .45);
}}

/* ---- the editor's own back arrow ---------------------------------------
   It pointed back to the module index, which is exactly what the page's
   breadcrumb above the card already does. Two back buttons a few pixels
   apart, one inside the editor and one outside it, is worse than one. The
   page keeps the breadcrumb; the editor drops its copy and reclaims the
   55px of gutter that was reserved for it. */
{ROOT_SELECTOR} .chooser > .top-btn:has(svg[title="back"]),
{ROOT_SELECTOR} .chooser > .back {{
  display: none;
}}
{ROOT_SELECTOR} .chooser.needed {{
  margin-left: 0 !important;
}}
"""

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
# `html`, `body` and `:root` all mean "the editor's root element" once the
# document becomes a div.
_ROOT_ELEMENT_RE = re.compile(r"^(?:html|body|:root)\b")


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep`, ignoring separators nested in brackets or strings.

    A selector list can contain commas inside `:is(...)`, `:not(...)` or an
    attribute value, and those must not be split on.
    """
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        if char == sep and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _scope_selector(selector: str) -> list[str]:
    """Rewrite one selector so it can only match inside the editor's container."""
    selector = selector.strip()
    if not selector:
        return []

    # `*` has to cover the container as well as its descendants, or a universal
    # reset (box-sizing, margin) would skip the root itself.
    if selector == "*":
        return [ROOT_SELECTOR, f"{ROOT_SELECTOR} *"]

    match = _ROOT_ELEMENT_RE.match(selector)
    if match:
        rest = selector[match.end() :]
        # `body.gui .side1` -> `#eu-editor.gui .side1`: the qualifier stays
        # attached to the root, which is what keeps the editor's mode classes
        # working now that they live on a div.
        return [f"{ROOT_SELECTOR}{rest}" if rest else ROOT_SELECTOR]

    return [f"{ROOT_SELECTOR} {selector}"]


def _scope_selector_list(selectors: str) -> str:
    out: list[str] = []
    for selector in _split_top_level(selectors):
        for scoped in _scope_selector(selector):
            if scoped not in out:
                out.append(scoped)
    return ", ".join(out)


def _find_block_end(css: str, open_index: int) -> int:
    """Index just past the `}` matching the `{` at `open_index`."""
    depth = 0
    quote: str | None = None
    i = open_index
    length = len(css)
    while i < length:
        char = css[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return length


def scope_css(css: str) -> str:
    """Prefix every rule in `css` so it applies only inside the container."""
    css = _COMMENT_RE.sub("", css)
    out: list[str] = []
    i = 0
    length = len(css)

    while i < length:
        if css[i].isspace():
            i += 1
            continue

        if css[i] == "@":
            # An at-rule ends at either `;` (@import, @charset) or a block.
            semicolon = css.find(";", i)
            brace = css.find("{", i)
            if brace == -1 or (semicolon != -1 and semicolon < brace):
                out.append(css[i : semicolon + 1] if semicolon != -1 else css[i:])
                i = semicolon + 1 if semicolon != -1 else length
                continue

            prelude = css[i:brace]
            end = _find_block_end(css, brace)
            keyword = prelude.split(None, 1)[0].lower()
            inner = css[brace + 1 : end - 1]
            if keyword in _NESTED_AT_RULES:
                # The newline is load-bearing. The dashboard renders this
                # fragment through Jinja, and a scoped rule placed straight
                # after the brace produces `{#eu-editor`, which Jinja reads as
                # the start of a comment and then fails to find the end of.
                out.append(f"{prelude.strip()} {{\n{scope_css(inner)}\n}}")
            else:
                # @keyframes, @font-face and friends: the body is not a
                # selector list, so it goes through as it is.
                out.append(css[i:end])
            i = end
            continue

        brace = css.find("{", i)
        if brace == -1:
            break
        end = _find_block_end(css, brace)
        selectors = css[i:brace]
        body = css[brace + 1 : end - 1]
        scoped = _scope_selector_list(selectors)
        if scoped:
            out.append(f"{scoped} {{{body}}}")
        i = end

    return "\n".join(out)


# The editor's own body script sets the placeholder on the Choices.js search
# field that the dashboard builds over the send form's <select>. In the
# standalone document that worked because the page pulled in the dashboard's
# script bundle *above* this line. Inlined, the host page owns that bundle and
# runs it after the content, so the field does not exist yet and the assignment
# throws. This waits for it instead.
_CHOICES_PLACEHOLDER_RE = re.compile(
    r"""document\.querySelector\(\s*(["'])input\[type=search\]\.choices__input\1\s*\)"""
    r"""\s*\.placeholder\s*=\s*([^;]+);""",
)

# Helpers, emitted *before* the container: the body carries an inline script of
# its own that runs while the container is still being parsed, so anything it
# calls has to already be defined.
_HELPERS_JS = """
var EU_ROOT = null;
// See _CHOICES_PLACEHOLDER_RE: the Choices field is built by the host page's
// own scripts, which run after this fragment, so give it a moment to appear.
function EU_setChoicesPlaceholder(text) {
  var tries = 0;
  (function attempt() {
    var field = document.querySelector("input[type=search].choices__input");
    if (field) { field.placeholder = text; return; }
    if (tries++ < 40) setTimeout(attempt, 50);
  })();
}
"""

# Emitted once the container exists, and before the editor's own scripts, which
# are the only things that read it.
_BIND_ROOT_JS = 'EU_ROOT = document.getElementById("{root_id}");'


def _rewrite_scripts(script: str) -> str:
    """Point the editor's `document.body` at its container instead.

    The editor drives its own UI state by toggling classes on `body`; with the
    document turned into a div those calls have to land on the div, or the
    GUI/JSON switch and the empty-embed placeholder stop responding.
    """
    # Global listeners stay global: narrowing this one to the container would
    # drop events that happen anywhere else on the page.
    script = script.replace("document.body.addEventListener", "document.addEventListener")
    script = _CHOICES_PLACEHOLDER_RE.sub(
        lambda m: f"EU_setChoicesPlaceholder({m.group(2).strip()});", script
    )
    return script.replace("document.body", "EU_ROOT")


def _extract(document: str) -> dict[str, t.Any]:
    head = document[: document.index("<body")]
    body_open = re.search(r"<body([^>]*)>", document)
    body = document[body_open.end() : document.rindex("</body>")]

    body_class = ""
    class_attr = re.search(r'class\s*=\s*"([^"]*)"', body_open.group(1) or "")
    if class_attr:
        body_class = class_attr.group(1)

    styles = [m.group(1) for m in re.finditer(r"<style[^>]*>(.*?)</style>", head, re.S)]
    links = re.findall(r'<link\b[^>]*rel="stylesheet"[^>]*>', head)

    scripts: list[str] = []
    for match in re.finditer(r"<script\b([^>]*)>(.*?)</script>", head, re.S):
        attrs, content = match.group(1), match.group(2)
        if "application/ld+json" in attrs:
            # Search-engine metadata for a page that no longer exists.
            continue
        if "src=" in attrs:
            scripts.append(match.group(0))
        else:
            scripts.append(f"<script{attrs}>{_rewrite_scripts(content)}</script>")

    # The editor's own copy of the dashboard's script bundle. The host page has
    # already loaded all of it; including it again re-runs jQuery, Bootstrap and
    # the dashboard's own initialisers on top of a live page.
    body = body.replace('{% include "includes/scripts.html" %}', "")
    body = re.sub(
        r"<script\b(?![^>]*\bsrc=)([^>]*)>(.*?)</script>",
        lambda m: f"<script{m.group(1)}>{_rewrite_scripts(m.group(2))}</script>",
        body,
        flags=re.S,
    )

    return {
        "body": body,
        "body_class": body_class,
        "styles": styles,
        "links": links,
        "scripts": scripts,
    }


def build_fragment() -> str:
    """The editor as a self-contained fragment for a dashboard module page."""
    with open(_EDITOR_PATH, encoding="utf-8") as file:
        document = file.read()

    parts = _extract(document)
    scoped_css = "\n".join(scope_css(block) for block in parts["styles"])

    fragment = "\n".join(
        [
            # Stylesheets first so the editor is never briefly unstyled.
            *parts["links"],
            f"<style>\n{scoped_css}\n{_INLINE_ADJUSTMENTS}\n</style>",
            f"<script>{_HELPERS_JS}</script>",
            f'<div id="{ROOT_ID}" class="{parts["body_class"]}">',
            parts["body"],
            "</div>",
            f"<script>{_BIND_ROOT_JS.format(root_id=ROOT_ID)}</script>",
            *parts["scripts"],
        ]
    )

    # The dashboard renders this through `render_template_string`, so a `{#`,
    # `{%` or `{{` introduced by the rewrite surfaces as a template error on the
    # page, a long way from the code that caused it. The original document is
    # the baseline: whatever Jinja syntax it already carried renders the same
    # way it always did, and anything above that count is ours.
    stray = _stray_jinja(fragment, document)
    if stray:
        raise RuntimeError(f"inlined editor produced stray Jinja syntax: {stray}")
    return fragment


def _stray_jinja(fragment: str, original: str) -> list[str]:
    """Jinja openers the rewrite introduced that the source document did not have."""
    found = []
    for token in ("{#", "{%", "{{"):
        surplus = fragment.count(token) - original.count(token)
        if surplus > 0:
            found.append(f"{token} x{surplus}")
    return found
