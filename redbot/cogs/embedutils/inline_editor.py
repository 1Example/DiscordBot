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

# Appended after the scoped stylesheet.
#
# The editor was written to own a browser window: it positions against the
# viewport, sizes panes in percentages, and pins a few things with hard-coded
# pixel offsets that only line up at the width it was designed for. Inside a
# dashboard card none of that holds, so this layer rebuilds the layout in terms
# of the space it is actually given, and moves the chrome onto the shell's
# palette. Grouped by what it is fixing.
_INLINE_ADJUSTMENTS = f"""
/* ---- 1. the container ---------------------------------------------------
   `.main` is `position: absolute; height: 100%`, and everything below it is a
   percentage of that, so the editor needs a positioned ancestor with a real
   height or the whole thing collapses. */
{ROOT_SELECTOR} {{
  /* `position: relative` stays: `.notification` and `.done` are absolutely
     positioned and need this as their containing block. The fixed height is
     gone - the card has room, so the editor grows to fit its content and the
     page scrolls, rather than becoming a short box with its own scrollbars. */
  position: relative;
  height: auto;
  min-height: 0;
  border-radius: 14px;
  overflow: visible;
  background: transparent;
  --fullEmbedBackground: transparent;
  --side1Background: transparent;
  --background-tertiary: rgba(255, 255, 255, .08);
}}
/* `.main` was `position: absolute; height: 100%`, which is what forced every
   pane below it to be a percentage of a fixed box. */
{ROOT_SELECTOR} .main {{
  position: relative;
  inset: auto;
  width: auto;
  height: auto;
  gap: 14px;
  align-items: stretch;
}}
/* Two even columns. The original 45/55 split was chosen against a full window;
   at card widths the editor column needs the same room as the preview. */
{ROOT_SELECTOR}:not(.no-preview):not(.no-editor) .main {{
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}}
@media (max-width: 1100px) {{
  /* Side by side stops working long before the card does; stack instead. */
  {ROOT_SELECTOR}:not(.no-preview):not(.no-editor) .main {{
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: minmax(0, 1fr) minmax(0, 1fr);
  }}
}}

/* ---- 2. the editor column ----------------------------------------------
   A column that fills its height, so the parts inside can be sized by how much
   room is left rather than by a percentage of the window. */
{ROOT_SELECTOR} .main .side1 {{
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  min-height: 0;
  padding: 12px;
  border: 1px solid var(--cx-panel-2-bd, rgba(120, 160, 255, .10));
  border-right: 1px solid var(--cx-panel-2-bd, rgba(120, 160, 255, .10));
  border-radius: 12px;
  background: var(--cx-panel-2, rgba(90, 130, 220, .07));
  height: auto;
  overflow: visible;
}}

/* The toolbar was a fixed 100px box holding seven controls, so they spilled
   out of it and scattered across the top of the page. */
{ROOT_SELECTOR} .chooser {{
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
  width: auto;
  height: auto;
  min-height: 38px;
  margin: 0;
  padding: 5px 6px;
  border-radius: 10px;
  background: var(--cx-panel-3, rgba(255, 255, 255, .045));
  border: 1px solid var(--cx-panel-3-bd, rgba(255, 255, 255, .08));
  box-shadow: none;
}}
/* The overflow menu belongs at the far end of the strip. */
{ROOT_SELECTOR} .chooser > .top-btn.menu {{ margin-left: auto; }}
{ROOT_SELECTOR} .chooser > .top-btn,
{ROOT_SELECTOR} .chooser > .pickerToggle {{
  position: static;
  flex: none;
  margin: 0;
}}

/* `height: 55%` left the editing area a fixed slab with dead space under it.
   It should take whatever the toolbar and the send bar do not. */
{ROOT_SELECTOR} .top {{
  flex: 1 1 auto;
  width: auto;
  height: auto;
  min-height: 0;
  margin: 0;
  padding: 0;
  /* No surface of its own: `.side1` already draws one, and the accordion rows
     inside carry theirs. Two nested frames read as a box in a box. */
  background: transparent;
  border: none;
  border-radius: 0;
  /* Nothing clips here any more, so the accordion is fully visible instead of
     being a scroll region inside a scroll region. */
  overflow: visible;
}}
/* The inner pane kept Discord's #292b2f, which was the last grey slab left in
   the editor column. */
{ROOT_SELECTOR} .top > .gui {{
  background: transparent;
  min-height: 0;
  padding: 2px 0 8px;
}}
{ROOT_SELECTOR} .editorHolder {{ min-height: 0; }}
/* JSON mode: a code surface should be the darkest thing in the column, but in
   the shell's blue rather than a flat near-black. */
{ROOT_SELECTOR} .CodeMirror {{
  background: rgba(4, 10, 32, .45);
  border-radius: 10px;
}}
{ROOT_SELECTOR} .CodeMirror-gutters {{
  background: transparent;
  border-right-color: var(--cx-panel-3-bd, rgba(255, 255, 255, .08));
}}

/* ---- 3. the send bar ----------------------------------------------------
   The colour picker and the send form are `position: absolute` pinned with
   hard-coded offsets (`top: 90px; left: 20px; width: 250px; height: 130px`),
   re-pinned again per editor mode. In a window that put them along the bottom;
   in a card it dropped them on top of the fields. They go back into flow as a
   footer row.

   `!important` is warranted here: the source pins them from several
   mode-specific selectors (`body.no-preview.gui .bottom .colors` and friends)
   that outrank anything reasonable to write, and they all need to lose. */
{ROOT_SELECTOR} .side1 > .bottom {{
  display: flex;
  align-items: stretch;
  gap: 10px;
  flex: none;
  width: 100%;
  max-width: none;
  margin: 0;
}}
{ROOT_SELECTOR} .side1 > .bottom > .colors,
{ROOT_SELECTOR} .side1 > .bottom > .sending {{
  position: static !important;
  top: auto !important;
  left: auto !important;
  right: auto !important;
  bottom: auto !important;
  width: auto !important;
  height: auto !important;
  margin: 0 !important;
  padding: 9px 11px;
  border-radius: 10px;
  background: var(--cx-panel-3, rgba(255, 255, 255, .045));
  border: 1px solid var(--cx-panel-3-bd, rgba(255, 255, 255, .08));
}}
{ROOT_SELECTOR} .side1 > .bottom > .colors {{ flex: 0 0 auto; }}
{ROOT_SELECTOR} .side1 > .bottom > .sending {{
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
}}
/* The send button carried its own 105px inline offset for the same reason. */
{ROOT_SELECTOR} .side1 > .bottom .sending input[type="submit"],
{ROOT_SELECTOR} .side1 > .bottom .sending button {{
  margin-left: auto !important;
  flex: none;
}}
{ROOT_SELECTOR} .side1 > .bottom .sending form {{
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-width: 0;
  margin: 0;
}}
{ROOT_SELECTOR} .side1 > .bottom .sending .choices,
{ROOT_SELECTOR} .side1 > .bottom .sending select {{
  flex: 1 1 auto;
  min-width: 0;
  margin: 0;
}}

/* ---- 4. the preview column ---------------------------------------------
   `.msgEmbed` is absolutely positioned and `.side2` was never a positioned
   element, so the preview escaped its column and anchored to the editor root -
   which is why it floated at a fixed spot with a gulf beside it. */
{ROOT_SELECTOR} .main .side2 {{
  position: relative;
  display: block;
  min-width: 0;
  min-height: 0;
  padding: 14px;
  border: 1px solid var(--cx-panel-2-bd, rgba(120, 160, 255, .10));
  border-radius: 12px;
  background: rgba(4, 10, 32, .35);
  height: auto;
  overflow: visible;
}}
{ROOT_SELECTOR} .msgEmbed {{
  position: relative;
  width: auto;
  margin: 0;
  /* Keeps the gutter the avatar is absolutely positioned into. */
  padding: 4px 8px 8px 64px;
}}
{ROOT_SELECTOR} .side2 .bottomSide {{ position: relative; }}

/* ---- 5. palette ---------------------------------------------------------
   The chrome moves onto the shell's surfaces. The embed preview deliberately
   does not: its job is to show what the embed will look like once posted, so
   it keeps Discord's own background and text colours. */
{ROOT_SELECTOR} .side1 .item.top,
{ROOT_SELECTOR} .top > .gui .item,
{ROOT_SELECTOR} .top > .gui .item ~ .edit,
{ROOT_SELECTOR} .side1 .bottom .box {{
  background-color: rgba(255, 255, 255, .04);
  border-color: var(--cx-panel-3-bd, rgba(255, 255, 255, .08));
}}
{ROOT_SELECTOR} .top > .gui .item:not(.inlineField):not(.guiEmbedName).active {{
  background-color: rgba(108, 140, 255, .18);
}}
/* The preview too. Keeping it Discord-grey made it a truthful mock-up of the
   posted result, but a grey slab in a blue card is the wrong trade here - the
   embed's own accent bar, fields and layout still show what you are building.
   Colours in the preview are no longer pixel-accurate to Discord. */
{ROOT_SELECTOR} .embed {{
  background: var(--cx-panel-2, rgba(90, 130, 220, .07));
  border-left-color: rgba(130, 175, 255, .45);
}}
{ROOT_SELECTOR} .msgEmbed .markup code,
{ROOT_SELECTOR} .embed code,
{ROOT_SELECTOR} .markup pre,
{ROOT_SELECTOR} .markup blockquote {{
  background: rgba(4, 10, 32, .5);
  border-color: var(--cx-panel-3-bd, rgba(255, 255, 255, .08));
}}
/* The message row above the embed sat on Discord's channel grey. */
{ROOT_SELECTOR} .msgEmbed,
{ROOT_SELECTOR} .msgEmbed > .contents,
{ROOT_SELECTOR} .side2 .bottomSide {{
  background: transparent;
}}

/* ---- 5b. the rest of Discord's greys ------------------------------------
   Enumerated by walking the rendered editor and collecting every element whose
   background was near-neutral, rather than by guessing at selectors. Four
   source greys, mapped onto the shell's three surfaces:

     #202225 (deepest)          -> the dark well
     #27282e #292b2f #212226    -> panel-3
     #2d2e33 #2d2f34 #35363e    -> a lift above panel-3
     #41444a                    -> the lightest chip
*/
{ROOT_SELECTOR} .notification,
{ROOT_SELECTOR} .CodeMirror-lint-tooltip {{
  background: rgba(4, 10, 32, .5);
}}
{ROOT_SELECTOR} .top-btn,
{ROOT_SELECTOR} .top-btn.menu > .box,
{ROOT_SELECTOR} .gui.opt,
{ROOT_SELECTOR} .json.opt,
{ROOT_SELECTOR} .guiEmbed,
{ROOT_SELECTOR} .fieldInner,
{ROOT_SELECTOR} .colLeft .picker,
{ROOT_SELECTOR} .CodeMirror-gutters {{
  background: var(--cx-panel-3, rgba(255, 255, 255, .045));
}}
{ROOT_SELECTOR} .designerFieldName,
{ROOT_SELECTOR} .item.pointer,
{ROOT_SELECTOR} .spinner-container,
{ROOT_SELECTOR} .item.toggle .inner .toggles .item,
{ROOT_SELECTOR} .top-btn.menu > .box .item.normal:hover {{
  background: rgba(255, 255, 255, .07);
}}
{ROOT_SELECTOR} .chooser > .back {{ background: rgba(255, 255, 255, .10); }}

/* The active GUI/JSON tab needs to read as selected against the new surface. */
{ROOT_SELECTOR} .chooser > .opt.selected,
{ROOT_SELECTOR} .chooser > .opt.active {{
  background: rgba(108, 140, 255, .28);
}}

/* CodeMirror ships white filler corners where its scrollbars meet. */
{ROOT_SELECTOR} .CodeMirror-scrollbar-filler,
{ROOT_SELECTOR} .CodeMirror-gutter-filler {{
  background: transparent;
}}

/* A bare <select> is white until Choices.js upgrades it, and stays white if
   Choices never runs. */
{ROOT_SELECTOR} select,
{ROOT_SELECTOR} .sending select {{
  background: var(--cx-panel-3, rgba(255, 255, 255, .045));
  color: var(--cx-text, #e6e9ef);
  border: 1px solid var(--cx-panel-3-bd, rgba(255, 255, 255, .08));
  border-radius: 8px;
  padding: 6px 9px;
}}
{ROOT_SELECTOR} select option {{ background: #0e1626; color: #e6e9ef; }}

/* The last few are set from compound selectors (`.fields+.edit .fieldInner
   .designerFieldName`, CodeMirror's own theme) that outrank a single-class
   override, so these are forced. Found by re-running the same sweep after the
   map above, not by guessing. */
{ROOT_SELECTOR} .fieldInner {{
  background: var(--cx-panel-3, rgba(255, 255, 255, .045)) !important;
}}
{ROOT_SELECTOR} .designerFieldName {{
  background: rgba(255, 255, 255, .07) !important;
}}
{ROOT_SELECTOR} .CodeMirror-gutters {{
  background: transparent !important;
}}

/* ---- 5c. scrollbars -----------------------------------------------------
   The sweep that found the grey surfaces only looked at element backgrounds,
   so it missed these: the editor styles its scrollbars through
   `::-webkit-scrollbar` pseudo-elements and `scrollbar-color`, in the same
   Discord greys (#36393f track, #202225 / #26272d / #222427 thumbs).

   With the panes no longer scrolling most of these never render, but the JSON
   editor and any narrow viewport still can, so they are themed rather than
   left as the last grey in the component. The source sets several with
   `!important`, which is why these match it. */
{ROOT_SELECTOR},
{ROOT_SELECTOR} * {{
  scrollbar-width: thin;
  scrollbar-color: rgba(130, 175, 255, .35) transparent;
}}
{ROOT_SELECTOR} ::-webkit-scrollbar,
{ROOT_SELECTOR} *::-webkit-scrollbar {{
  width: 9px;
  height: 9px;
  background: transparent !important;
}}
{ROOT_SELECTOR} ::-webkit-scrollbar-track,
{ROOT_SELECTOR} *::-webkit-scrollbar-track {{
  background: transparent !important;
}}
{ROOT_SELECTOR} ::-webkit-scrollbar-thumb,
{ROOT_SELECTOR} *::-webkit-scrollbar-thumb {{
  background: rgba(130, 175, 255, .30) !important;
  border-radius: 999px;
  border: 2px solid transparent;
  background-clip: padding-box !important;
}}
{ROOT_SELECTOR} ::-webkit-scrollbar-thumb:hover,
{ROOT_SELECTOR} *::-webkit-scrollbar-thumb:hover {{
  background: rgba(130, 175, 255, .50) !important;
}}
{ROOT_SELECTOR} ::-webkit-scrollbar-corner,
{ROOT_SELECTOR} *::-webkit-scrollbar-corner {{
  background: transparent !important;
}}

/* CodeMirror sizes itself to a fixed height and scrolls inside by default.
   Letting it grow keeps the JSON tab consistent with the GUI one - one page,
   one scrollbar, and that one belongs to the browser. */
{ROOT_SELECTOR} .CodeMirror {{
  height: auto;
}}
{ROOT_SELECTOR} .CodeMirror-scroll {{
  max-height: none;
  min-height: 340px;
  overflow-y: hidden !important;
  overflow-x: auto;
}}

/* ---- 6. the editor's own back arrow -------------------------------------
   It pointed back to the module index, which is what the page's breadcrumb
   above the card already does. Two back buttons a few pixels apart is worse
   than one, so the editor drops its copy and reclaims the gutter reserved for
   it. */
{ROOT_SELECTOR} .chooser > .top-btn:has(svg[title="back"]),
{ROOT_SELECTOR} .chooser > .back {{
  display: none;
}}
{ROOT_SELECTOR} .chooser.needed {{ margin-left: 0 !important; }}

/* The shell's form rules stop at the editor's edge; its controls are styled by
   the stylesheet above. */
{ROOT_SELECTOR} input,
{ROOT_SELECTOR} select,
{ROOT_SELECTOR} textarea,
{ROOT_SELECTOR} button {{
  font-family: inherit;
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
