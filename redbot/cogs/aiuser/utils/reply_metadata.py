"""Remove reply-context labels accidentally imitated by the model."""

import re


_REPLY_PREFIX = re.compile(r"^\s*\(replying to\s+", re.IGNORECASE)


def strip_reply_context_prefix(text: str) -> str:
    """Strip leading labels only, retaining normal prose and quoted examples.

    Balance parentheses outside double-quoted snippets so a reply to
    ``"the event (tomorrow)?"`` doesn't leave part of the label behind.
    Incomplete labels are preserved instead of risking removal of answer text.
    """
    while match := _REPLY_PREFIX.match(text):
        depth = 1
        quoted = False
        escaped = False
        for index in range(match.end(), len(text)):
            char = text[index]
            if escaped:
                escaped = False
                continue
            if quoted and char == "\\":
                escaped = True
                continue
            if char == '"':
                quoted = not quoted
            elif not quoted:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        text = text[index + 1:].lstrip()
                        break
        else:
            return text
    return text
