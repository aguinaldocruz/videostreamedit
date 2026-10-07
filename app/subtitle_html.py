"""Classify removable subtitle HTML, preserving color, italics, underline and breaks."""

import html
import re

MARKUP_VERSION = 6

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
    """Return color/italic/underline/break text and whether removable markup existed.

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
        if name == "br":
            # A line break is a void element: never push/pop the styling
            # stack. Accept the closing form found in some subtitle files
            # too, without treating it as an unmatched presentation tag.
            attributes = match.group(3)
            if attributes.strip(" /\t\r\n"):
                removable = True
            pieces.append("</br>" if closing else "<br/>" if attributes.rstrip().endswith("/") else "<br>")
            continue
        if closing:
            # Find the matching opening, including around malformed nested tags.
            index = next((i for i in range(len(stack) - 1, -1, -1) if stack[i][0] == name), -1)
            if index < 0:
                removable = True
                continue
            retained = stack[index][1]
            del stack[index:]
            if retained:
                pieces.append(f"</{'i' if name == 'em' else name}>")
            else:
                removable = True
            continue
        opening = f"<{'i' if name == 'em' else name}>" if name in {"i", "em", "u"} else _color_opening(name, match.group(3))
        retained = bool(opening)
        if retained:
            pieces.append(opening)
            # Whitelisted styling is safe; extra attributes and CSS are not.
            attributes = match.group(3)
            if name in {"i", "em", "u"}:
                remainder = attributes
            elif name == "font":
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
        if match.group(3).rstrip().endswith("/"):
            if retained:
                pieces.append(f"</{'i' if name == 'em' else name}>")
        else:
            stack.append((name, retained))
    pieces.append(text[position:])
    return "".join(pieces), removable


def has_removable_html(text: str) -> bool:
    return color_only_html(text)[1]


def strip_non_color_html(text: str) -> str:
    cleaned, _ = color_only_html(text)
    return html.unescape(cleaned)


def refresh_cached_markup() -> dict:
    """Upgrade findings from complete cache only; no extraction or catalog jobs.

    Extending the tag whitelist cannot create a new finding. Old
    non-HTML findings remain valid. HTML findings require current cached text;
    unverified/uncached ones stay old and wait for ordinary subtitle inspection.
    """
    import logging
    from app.pg_compat import connect
    from app.subtitle_cache import CACHE_FORMAT_VERSION
    from app.v51 import markup_kind, TEXT_SUBTITLE_CODECS
    logger = logging.getLogger('uvicorn.error')
    updated = 0
    with connect() as db:
        tables = db.execute("SELECT to_regclass('subtitle_extended_media') AS inspection,to_regclass('media_stream_index_state') AS core").fetchone()
        if tables['inspection'] is None or tables['core'] is None:
            return {'unchanged_media': 0, 'cached_media': 0}
        unchanged = db.execute(
            "UPDATE subtitle_extended_media m SET markup_version=? WHERE markup_version>=3 AND markup_version<? "
            "AND NOT EXISTS (SELECT 1 FROM subtitle_extended_index s WHERE s.path=m.path AND s.markup LIKE '%HTML tags%')",
            (MARKUP_VERSION, MARKUP_VERSION),
        ).rowcount
    cursor = ''
    while True:
        with connect() as db:
            media = db.execute(
                "SELECT m.path,m.indexed_at FROM subtitle_extended_media m "
                "JOIN subtitle_cache_media c ON c.path=m.path "
                "JOIN media_stream_index_state i ON i.path=m.path "
                "WHERE m.markup_version>=3 AND m.markup_version<? AND m.path>? AND c.format_version=? "
                "AND c.expected_tracks=c.cached_tracks AND m.size=i.size "
                "AND m.modified=i.modified_ns/1000000000 "
                "AND c.cached_at>=CAST(i.indexed_at AS TIMESTAMPTZ) "
                "AND NOT EXISTS (SELECT 1 FROM subtitle_cache_pending q WHERE q.path=m.path) "
                "ORDER BY m.path LIMIT 50", (MARKUP_VERSION, cursor, CACHE_FORMAT_VERSION),
            ).fetchall()
        if not media:
            break
        for item in media:
            path = item['path']
            with connect() as db:
                rows = db.execute(
                    "SELECT s.source,s.type_index,s.external_path,s.codec,t.text_content "
                    "FROM subtitle_extended_index s LEFT JOIN subtitle_cache_track t "
                    "ON t.path=s.path AND t.source=s.source AND t.type_index=s.type_index "
                    "AND t.external_path=s.external_path WHERE s.path=?", (path,),
                ).fetchall()
                text_rows = [row for row in rows if str(row['codec']).lower() in TEXT_SUBTITLE_CODECS]
                if any(row['text_content'] is None for row in text_rows):
                    continue
                # Lock the old inspection stamp: a concurrent index refresh
                # must not receive findings from an earlier cache snapshot.
                claimed = db.execute(
                    "UPDATE subtitle_extended_media SET markup_version=? WHERE path=? AND markup_version>=3 AND markup_version<? AND indexed_at=?",
                    (MARKUP_VERSION, path, MARKUP_VERSION, item['indexed_at']),
                ).rowcount
                if not claimed:
                    continue
                db.executemany(
                    "UPDATE subtitle_extended_index SET markup=? WHERE path=? AND source=? AND type_index=? AND external_path=?",
                    [(markup_kind(row['text_content']), path, row['source'], row['type_index'], row['external_path']) for row in text_rows],
                )
                updated += 1
        cursor = media[-1]['path']
    logger.info('subtitle_html event=policy_updated unchanged_media=%d cached_media=%d policy=color_italics_underline_and_breaks', unchanged, updated)
    return {'unchanged_media': unchanged, 'cached_media': updated}
