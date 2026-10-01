"""Turn a Telegram message (text/caption + entities) back into the HTML a draft was written in."""
from html import escape

TAGS = {"bold": "b", "italic": "i", "underline": "u", "strikethrough": "s", "code": "code", "spoiler": "tg-spoiler"}


def _utf16_units(s):
    return [c for ch in s for c in ([ch] if ord(ch) < 0x10000 else [ch, ""])]


def to_html(message):
    """Return (html, media, media_type) for a draft message."""
    text = message.get("caption", message.get("text", ""))
    entities = message.get("caption_entities", message.get("entities", []))
    units = _utf16_units(text)  # entity offsets are in UTF-16 code units
    opens, closes = {}, {}
    for e in entities:
        if e["type"] == "text_link":
            tag = (f'<a href="{escape(e["url"])}">', "</a>")
        elif e["type"] in TAGS:
            tag = (f"<{TAGS[e['type']]}>", f"</{TAGS[e['type']]}>")
        else:
            continue
        opens.setdefault(e["offset"], []).append(tag[0])
        closes.setdefault(e["offset"] + e["length"], []).insert(0, tag[1])
    out = []
    for i, ch in enumerate(units + [""]):
        out += closes.get(i, []) + opens.get(i, [])
        out.append(escape(ch, quote=False))
    html = "".join(out)

    if "photo" in message:
        return html, message["photo"][-1]["file_id"], "photo"
    if "video" in message:
        return html, message["video"]["file_id"], "video"
    url = (message.get("link_preview_options") or {}).get("url")
    return html, url, "link" if url else None
