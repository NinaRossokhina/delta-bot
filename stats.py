"""Weekly channel stats: which posts got the most views and reactions.

Telegram's Bot API does not give the views of channel posts, so they are read from the channel's
public web preview https://t.me/s/<channel> (the same counters anyone sees there, no login needed).
Run on Mondays by the "Weekly stats" workflow: sends Nina the week's top posts and appends one line
per post to stats.jsonl, which ai.py reads to pick topics readers like.

Usage: python stats.py
"""
import html
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta

from schedule import MSK, now_msk

STATS = "stats.jsonl"  # {"week": "2026-10-05", "id", "date", "title", "views", "reactions"} per line, append-only
PAGES = 15  # ~20 posts per page; enough for a week of 5-7 posts a day
TOP = 5


def number(text):
    """'1.2K' -> 1200, '3M' -> 3000000, '845' -> 845."""
    m = re.fullmatch(r"([\d.,]+)\s*([KM]?)", text.strip(), re.I)
    if not m:
        return 0
    value = float(m[1].replace(",", "."))
    return int(round(value * {"": 1, "K": 1000, "M": 1_000_000}[m[2].upper()]))


def text_of(fragment):
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def parse(page):
    """Posts on one t.me/s page: [{"id", "date" (Moscow, 'YYYY-MM-DDTHH:MM'), "title", "views", "reactions"}]."""
    posts = []
    for block in re.split(r'(?=<div class="tgme_widget_message_wrap)', page)[1:]:
        post = re.search(r'data-post="[^"/]+/(\d+)"', block)
        when = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        if not post or not when:
            continue
        views = re.search(r'class="tgme_widget_message_views">([^<]*)<', block)
        body = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        reactions = 0
        box = re.search(r'class="tgme_widget_message_reactions[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if box:
            # Each reaction is an emoji (an image or a character inside tags) followed by its count.
            for count in re.findall(r"</(?:i|b|tg-emoji)>\s*([\d.,]+[KM]?)", box[1], re.I):
                reactions += number(count)
        text = text_of(body[1]) if body else ""
        title = next((line for line in text.split("\n") if line.strip()), "(пост без текста)")
        date = datetime.fromisoformat(when[1].replace("Z", "+00:00")).astimezone(MSK)
        posts.append({"id": int(post[1]), "date": date.strftime("%Y-%m-%dT%H:%M"), "title": title.strip()[:120],
                      "views": number(views[1]) if views else 0, "reactions": reactions})
    return posts


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; DeltaBot/1.0)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def week_posts(channel, days=7, get=fetch):
    """Posts of the last `days` days, newest pages first (t.me/s/<channel>?before=<id> for older ones)."""
    since = (datetime.fromisoformat(now_msk()) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
    found, before = {}, None
    for _ in range(PAGES):
        raw = get(f"https://t.me/s/{channel}" + (f"?before={before}" if before else ""))
        page = parse(raw)
        if not page and before is None:
            raise RuntimeError("на странице t.me/s/" + channel + " не нашлось постов (Telegram поменял страницу?)")
        if not page:
            break
        for p in page:
            if p["date"] >= since:
                found[p["id"]] = p
        oldest = min(p["id"] for p in page)
        if min(p["date"] for p in page) < since or (before and oldest >= before):
            break
        before = oldest
    return sorted(found.values(), key=lambda p: p["id"])


def short(title, limit=70):
    return title if len(title) <= limit else title[:limit - 1].rstrip() + "…"


def report(posts, channel):
    if not posts:
        return "За неделю в канале не нашлось постов (или Telegram не показал их на странице канала)."
    link = lambda p: f'<a href="https://t.me/{channel}/{p["id"]}">{html.escape(short(p["title"]))}</a>'
    lines = [f"<b>Итоги недели</b>: постов {len(posts)}, просмотров {sum(p['views'] for p in posts)}, "
             f"реакций {sum(p['reactions'] for p in posts)}.", "", "<b>Больше всего просмотров</b>"]
    by_views = sorted(posts, key=lambda p: (-p["views"], -p["reactions"]))[:TOP]
    lines += [f"{i}. {link(p)} — {p['views']} просм., {p['reactions']} реакц." for i, p in enumerate(by_views, 1)]
    by_reactions = [p for p in sorted(posts, key=lambda p: (-p["reactions"], -p["views"])) if p["reactions"]][:TOP]
    if by_reactions:
        lines += ["", "<b>Больше всего реакций</b>"]
        lines += [f"{i}. {link(p)} — {p['reactions']} реакц., {p['views']} просм." for i, p in enumerate(by_reactions, 1)]
    else:
        lines += ["", "Реакций на странице канала не видно (может быть, они выключены в канале)."]
    lines += ["", "Свежие посты (вчерашние и сегодняшние) ещё добирают просмотры. "
                  "Эту статистику я учитываю, когда выбираю темы для новых постов."]
    return "\n".join(lines)


def save(posts):
    week = now_msk()[:10]
    with open(STATS, "a", encoding="utf-8") as f:
        for p in posts:
            f.write(json.dumps({"week": week, **p}, ensure_ascii=False) + "\n")


def for_prompt(weeks=4, top=8, bottom=5):
    """What readers liked lately, for ai.py: the most and least read posts of the last few weekly runs."""
    try:
        with open(STATS, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return ""
    since = (datetime.fromisoformat(now_msk()) - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
    rows = sorted((r for r in rows if r.get("week", "") >= since), key=lambda r: r["week"])
    latest = {r["id"]: r for r in rows}  # a later run's numbers win
    posts = sorted(latest.values(), key=lambda p: (-p["views"], -p["reactions"]))
    if len(posts) < 3:
        return ""
    row = lambda p: f"- {p['title']} ({p['views']} views, {p['reactions']} reactions)"
    best = "\n".join(row(p) for p in posts[:top])
    worst = "\n".join(row(p) for p in posts[top:][-bottom:])
    return f"Most read:\n{best}" + (f"\nLeast read:\n{worst}" if worst else "")


def main():
    from tg import call
    channel = os.environ["CHANNEL_ID"].lstrip("@")
    posts = week_posts(channel)
    save(posts)
    call("sendMessage", chat_id=os.environ["ADMIN_CHAT_ID"], text=report(posts, channel),
         parse_mode="HTML", link_preview_options={"is_disabled": True})
    print(f"{len(posts)} posts this week")


if __name__ == "__main__":
    main()
