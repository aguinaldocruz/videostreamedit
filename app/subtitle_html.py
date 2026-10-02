"""Classify subtitle HTML and remove markup without losing color styling."""

import html
import re


TAG = re.compile(r"<\s*(/?)\s*([a-z][a-z0-9]*)\b([^<>]*?)>", re.IGNORECASE)
PRESENTATION_TAGS = frozenset({
    "i", "b", "u", "s", "em", "strong", "font", "span", "br", "div",
    "p", "ruby", "rt", "rb", "c", "q", "small", "big", "sub", "sup",
    "a", "nobr", "strike", "tt", "v", "lang", "center", "marquee",
    "blink", "mark", "del", "ins", "code", "pre", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "td", "th", "caption",
})
COLOR_ATTRIBUTE = re.compile(r"(?:^|\s)color\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s/>]+))", re.IGNORECASE)
STYLE_ATTRIBUTE = re.compile(r"(?:^|\s)style\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)
COLOR_DECLARATION = re.compile(r"(?:^|;)\s*color\s*:\s*([^;]+)", re.IGNORECASE)
SAFE_COLOR = re.compile(r"(?:#[0-9a-f]{3,8}|[a-z]{1,32}|rgba?\([\d.,%\s]+\)|hsla?\([\d.,%\s]+\))\Z", re.IGNORECASE)


def _color_opening(name: str, attributes: str) -> str:
    if name == "font":
        match = COLOR_ATTRIBUTE.search(attributes)
        color = next((value for value in match.groups() if value is not None), "") if match else ""
    elif name == "span":
        match = STYLE_ATTRIBUTE.search(attributes)
        style = next((value for value in match.groups() if value is not None), "") if match else ""
        declaration = COLOR_DECLARATION.search(style)
        color = declaration.group(1).strip() if declaration else ""
    else:
        return ""
    color = html.unescape(color).strip()
    if not SAFE_COLOR.fullmatch(color):
        return ""
    return f'<font color="{color}">' if name == "font" else f'<span style="color: {color}">'


def color_only_html(text: str) -> tuple[str, bool]:
    """Return color-only text and whether removable presentation markup existed.

    Only known presentation tags are treated as HTML: dialogue such as
    ``<John>`` must not become a report finding or disappear during cleanup.
    """
    pieces = []
    stack: list[tuple[str, bool]] = []
    removable = False
    position = 0
    for match in TAG.finditer(text):
        name = match.group(2).lower()
        if name not in PRESENTATION_TAGS:
            continue
        pieces.append(text[position:match.start()])
        position = match.end()
        closing = bool(match.group(1))
        if closing:
            # Find the matching opening, including around malformed nested tags.
            index = next((i for i in range(len(stack) - 1, -1, -1) if stack[i][0] == name), -1)
            if index < 0:
                removable = True
                continue
            retained = stack[index][1]
            del stack[index:]
            if retained:
                pieces.append(f"</{name}>")
            else:
                removable = True
            continue
        opening = _color_opening(name, match.group(3))
        retained = bool(opening)
        if retained:
            pieces.append(opening)
            # Non-color attributes and CSS declarations are removable markup.
            attributes = match.group(3)
            if name == "font":
                remainder = COLOR_ATTRIBUTE.sub("", attributes, count=1)
            else:
                remainder = STYLE_ATTRIBUTE.sub("", attributes, count=1)
                style_match = STYLE_ATTRIBUTE.search(attributes)
                style = next((value for value in style_match.groups() if value is not None), "") if style_match else ""
                if COLOR_DECLARATION.sub("", style).strip(" ;"):
                    removable = True
            if remainder.strip(" /\t\r\n"):
                removable = True
        else:
            removable = True
        if match.group(3).rstrip().endswith("/") or name == "br":
            if retained:
                pieces.append(f"</{name}>")
        else:
            stack.append((name, retained))
    pieces.append(text[position:])
    return "".join(pieces), removable


def has_removable_html(text: str) -> bool:
    return color_only_html(text)[1]


def strip_non_color_html(text: str) -> str:
    cleaned, _ = color_only_html(text)
    return html.unescape(cleaned)
