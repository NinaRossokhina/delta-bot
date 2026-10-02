"""Hot news: a global AI story (a new OpenAI or Anthropic model and the like) comes to Nina as a draft
within minutes, so she can publish it with «Сейчас» while everyone is talking about it.

Runs from the "Hot news" workflow (hot.yml) every few minutes. Three steps, the expensive one only
when it is needed:
1. Free: read the sources (official blogs of the AI labs, Techmeme, the Hacker News front page) and
   keep only items not seen before (`.hot/seen.json`, kept in the Actions cache, not in the repo).
2. Cheap: one request to a small model (CHEAP_MODEL) rates the new items; it is skipped when there
   is nothing new.
3. Expensive: only for a story rated HOT_MIN or higher, the usual model with web search checks it and
   writes the post, which is sent as an ordinary draft and saved to drafts/<day>-hot.json (so the
   daily drafts do not repeat it, and the next hot story is compared with it).
The first run without saved state only remembers what is there (no flood of old news).
"""
import email.utils
import html
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import ai
import costs
import polza
from schedule import next_free_slot, now_msk
from send_drafts import send
from tg import call

STATE = ".hot"  # restored from and saved to the Actions cache by hot.yml
SEEN = f"{STATE}/seen.json"  # {item id: "YYYY-MM-DDTHH:MM" first seen}
NOTES = f"{STATE}/notes.json"  # {"no_money": "YYYY-MM-DD"}: problems already reported today
PENDING_COSTS = f"{STATE}/costs.jsonl"  # prices of the cheap requests, moved to costs.jsonl now and then
CHEAP_MODEL = "anthropic/claude-haiku-4.5"
HOT_MIN = 8  # score 1-10 from the cheap model; only a story everyone is talking about gets 8+
MAX_POSTS = 2  # hot posts per run at most
MAX_AGE_HOURS = 12  # older items are not news any more
HN_POINTS = 300  # a Hacker News story counts once it has this many points
KEEP_DAYS = 90  # how long seen items are remembered
FLUSH_HOURS = 6  # cheap requests' prices go to costs.jsonl at least this often (one commit, not one per run)
MAX_ITEMS = 40  # new items rated in one request at most

# (name, kind, url, keywords only): official lab blogs pass as is, general tech news only with AI words
SOURCES = [
    ("OpenAI", "rss", "https://openai.com/news/rss.xml", False),
    ("Anthropic", "anthropic", "https://www.anthropic.com/news", False),
    ("Google DeepMind", "rss", "https://deepmind.google/blog/rss.xml", False),
    ("Google AI", "rss", "https://blog.google/technology/ai/rss/", False),
    ("Techmeme", "rss", "https://www.techmeme.com/feed.xml", True),
    ("Hacker News", "hn", "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=60", True),
]
KEYWORDS = re.compile(
    r"\bAI\b|(?i:\b(openai|chatgpt|gpt-?\d|sora|anthropic|claude|gemini|deepmind|llama|xai|grok|mistral|deepseek|"
    r"qwen|nvidia|copilot|llm|neural|robot\w*|agi|artificial intelligence|machine learning|model)\b)")

RATE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "i": {"type": "integer", "description": "Number of the item in the list."},
            "score": {"type": "integer", "description": "1-10, see the scale."},
            "covered": {"type": "boolean", "description": "The same story is already among the channel's recent drafts."},
        },
        "required": ["i", "score", "covered"],
        "additionalProperties": False,
    }}},
    "required": ["items"],
    "additionalProperties": False,
}
RATE_TASK = """You watch the news for Delta, a Russian Telegram channel with positive AI and technology news. Rate how big each new item below is, so that only global breaking news reaches the channel immediately.

Scale:
- 9-10: a story the whole internet is talking about right now: a new flagship model or major product from OpenAI, Anthropic, Google, Meta, xAI, Apple, DeepSeek and the like (GPT-6, a new Claude, a new Gemini), a historic breakthrough.
- 7-8: important news of a big company that many tech media will cover today, but not a global event.
- 1-6: everything else: customer stories, small updates, research papers, opinion pieces, events, hiring, funding of small startups.
- Score 3 or less for negative stories (scandals, lawsuits, layoffs, outages, safety incidents): the channel is positive only.
covered = true if the same story (same launch, even in other words or in Russian) is among the recent drafts.

Recent drafts of the channel:
{recent}

New items:
{items}

Rate every item."""

HOT_SCHEMA = {
    "type": "object",
    "properties": {
        "skip": {"type": "boolean", "description": "true if after checking the story is not new, not major, not positive or already covered; then the other fields are empty strings."},
        "text": {"type": "string", "description": "The post in Telegram HTML: <b>headline</b>, short paragraphs separated by \\n\\n, source woven into the text as <a href='...'>word</a>, length per the style guide."},
        "image": {"type": "string", "description": "Direct .jpg/.png image URL for the post, or an empty string."},
        "source": {"type": "string", "description": "URL of the primary source the facts were checked against."},
    },
    "required": ["skip", "text", "image", "source"],
    "additionalProperties": False,
}
HOT_TASK = """Breaking news that is spreading right now: «{title}» ({source}, {url}).
Write one post about it for the channel while it is hot.
- Check it against the primary source (fetch_page; the official announcement if there is one) and use the freshest details from web search.
- Strictly follow the style guide (voice and length), link the source as a hyperlinked word, add a direct .jpg/.png image URL you saw on the source page (og:image is ideal).
- If it turns out to be old, minor, negative or already among the recent drafts, return skip = true.
Return JSON in the given format (not through send_drafts: the bot sends it itself)."""
INTRO = ("🔥 Срочная новость, её сейчас обсуждают все. Нажми «Сейчас», чтобы выпустить пост сразу, "
         "или выбери время.")
FAILED = "Нашла срочную новость «{}», но пост не собрался: {}"


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; DeltaBot/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(3_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")


def when(text):
    """A feed date (RFC 822 or ISO 8601) as an aware datetime, or None."""
    if not text:
        return None
    text = text.strip()
    try:
        d = email.utils.parsedate_to_datetime(text)
        if d is None:
            raise ValueError(text)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(node, name):
    return next((c for c in node if _local(c.tag) == name), None)


def parse_feed(xml):
    """Items of an RSS 2.0 or Atom feed: [{"title", "url", "id", "published"}]."""
    items = []
    for node in ET.fromstring(xml).iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title, link = _child(node, "title"), _child(node, "link")
        url = (link.get("href") or link.text or "").strip() if link is not None else ""
        guid = next((c for c in node if _local(c.tag) in ("guid", "id")), None)
        date = next((c.text for c in node if _local(c.tag) in ("pubDate", "published", "updated", "date")), None)
        items.append({"title": re.sub(r"<[^>]+>", "", html.unescape((title.text or "") if title is not None else "")).strip(),
                      "url": url, "id": ((guid.text or "").strip() if guid is not None else "") or url,
                      "published": when(date)})
    return items


def parse_anthropic(page):
    """anthropic.com/news has no feed: links /news/<slug>, the title from the link text or the slug."""
    items, found = [], set()
    for m in re.finditer(r'<a\b[^>]*href="(/news/[a-z0-9][a-z0-9-]*)"[^>]*>(.*?)</a>', page, re.S | re.I):
        path = m[1]
        if path in found:
            continue
        found.add(path)
        heading = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", m[2], re.S)
        title = html.unescape(re.sub(r"<[^>]+>", " ", heading[1] if heading else "")).strip()
        title = " ".join(title.split()) or path.rsplit("/", 1)[1].replace("-", " ").capitalize()
        url = "https://www.anthropic.com" + path
        items.append({"title": title, "url": url, "id": url, "published": None})
    return items


def parse_hn(data, points=HN_POINTS):
    """Hacker News front page stories with at least `points` points."""
    items = []
    for hit in json.loads(data).get("hits", []):
        if (hit.get("points") or 0) < points or not hit.get("title"):
            continue
        published = datetime.fromtimestamp(hit["created_at_i"], timezone.utc) if hit.get("created_at_i") else None
        items.append({"title": hit["title"], "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}",
                      "id": f"hn:{hit['objectID']}", "published": published})
    return items


PARSERS = {"rss": parse_feed, "anthropic": parse_anthropic, "hn": parse_hn}


def collect(sources=SOURCES, fetch=get):
    """Current items of all sources: [{"source", "title", "url", "id", "published"}]. A source that
    fails is skipped (printed), the others still count."""
    items = []
    for name, kind, url, keywords in sources:
        try:
            found = PARSERS[kind](fetch(url))
        except Exception as e:
            print(f"{name}: {type(e).__name__} {str(e)[:150]}")
            continue
        for item in found:
            if item["title"] and (not keywords or KEYWORDS.search(item["title"])):
                items.append({"source": name, **item, "id": f"{name}:{item['id']}"})
        print(f"{name}: {len(found)} items")
    return items


def fresh(item, now):
    return item["published"] is None or now - item["published"] <= timedelta(hours=MAX_AGE_HOURS)


def rate(items):
    """Scores of the new items from the cheap model: {index: (score, covered)}."""
    listing = "\n".join(f"{i}. [{x['source']}] {x['title']} ({x['url']})" for i, x in enumerate(items))
    prompt = RATE_TASK.format(recent=ai.recent_headlines(days=3) or "(none)", items=listing)
    choice = polza.chat([{"role": "user", "content": prompt}], model=CHEAP_MODEL, max_tokens=3000,
                        response_format=polza.json_schema("rated_items", RATE_SCHEMA))
    rated = json.loads(polza.text(choice.message))["items"]
    return {r["i"]: (r["score"], r["covered"]) for r in rated if 0 <= r["i"] < len(items)}


def write_post(item):
    """The post about a hot story from the usual model with web search, or None if it says skip."""
    tools = [t for t in ai.TOOLS if t["function"]["name"] == "fetch_page"]
    messages = [{"role": "system", "content": ai.system_prompt()},
                {"role": "user", "content": HOT_TASK.format(**item)}]
    for _ in range(12):
        choice = polza.chat(messages, tools=tools, web=True, response_format=polza.json_schema("hot_post", HOT_SCHEMA))
        message = choice.message
        if choice.finish_reason == "content_filter" or getattr(message, "refusal", None):
            raise RuntimeError("the model refused")
        messages.append(polza.assistant_message(message))
        if message.tool_calls:
            for tool_call in message.tool_calls:
                result, _ = ai.use_tool(tool_call, {"kind": "hot"})
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": result})
            continue
        post = json.loads(polza.text(message))
        return None if post["skip"] or not post["text"].strip() else post
    raise RuntimeError("no answer after 12 steps")


def hot_path(day):
    """drafts/<day>-hot.json, or -hot-2.json etc. if taken."""
    path, i = f"drafts/{day}-hot.json", 1
    while os.path.exists(path):
        i += 1
        path = f"drafts/{day}-hot-{i}.json"
    return path


def deliver(post):
    """Send the post as an ordinary draft (suggested time: the nearest free slot) and save it."""
    at = next_free_slot(ai.queued_times(), now_msk())
    draft = {"text": post["text"], "media": post["image"] or None, "media_type": "photo" if post["image"] else "none",
             "time": at[11:], "suggested": True}
    call("sendMessage", chat_id=ai.ADMIN, text=INTRO)
    try:
        send(draft, at[:10])
    except Exception as e:  # a bad image must not lose the news
        print(f"send failed: {e}; retrying without media")
        send({**draft, "media": None, "media_type": "none"}, at[:10])
    path = hot_path(now_msk()[:10])
    with open(path, "w", encoding="utf-8") as f:
        json.dump([draft], f, ensure_ascii=False, indent=1)
        f.write("\n")
    return path


def flush_costs(force=False):
    """Move the cheap requests' prices to costs.jsonl (for /cost) when a post was made or the oldest
    is FLUSH_HOURS old; returns True if costs.jsonl changed."""
    try:
        with open(PENDING_COSTS, encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
    except FileNotFoundError:
        return False
    if not lines:
        return False
    try:
        oldest = datetime.fromisoformat(json.loads(lines[0])["t"])
    except (ValueError, KeyError):
        oldest = datetime.min
    if not force and datetime.fromisoformat(now_msk()) - oldest < timedelta(hours=FLUSH_HOURS):
        return False
    with open("costs.jsonl", "a", encoding="utf-8") as f:
        f.writelines(lines)
    os.remove(PENDING_COSTS)
    return True


def report_once(notes, key, text):
    """Tell Nina about a problem at most once a day (the check runs every few minutes)."""
    today = now_msk()[:10]
    if notes.get(key) != today:
        ai.notify("sendMessage", chat_id=ai.ADMIN, text=text)
        notes[key] = today


def check(sources=SOURCES, fetch=get):
    """One round: find new items, rate them, write and send posts for the hot ones. Returns the paths
    of saved hot drafts."""
    now = datetime.now(timezone.utc)
    seen = load(SEEN, None)
    notes = load(NOTES, {})
    items = collect(sources, fetch)
    first_run = seen is None
    seen = seen or {}
    stamp = now_msk()
    new = [x for x in items if x["id"] not in seen]
    for x in new:
        if first_run or not fresh(x, now):
            seen[x["id"]] = stamp
    new = [] if first_run else [x for x in new if fresh(x, now)][:MAX_ITEMS]
    if first_run:
        print(f"first run: remembered {len(seen)} items")
    paths = []
    if new:
        try:
            scores = rate(new)
        except Exception as e:
            print(f"rating failed: {type(e).__name__} {str(e)[:200]}")  # the items stay new, tried again next run
            if ai.problem(e) == ai.NO_MONEY:
                report_once(notes, "no_money", ai.NO_MONEY)
            scores = None
        if scores is not None:
            for x in new:
                seen[x["id"]] = stamp
            hot = sorted(((s, i) for i, (s, covered) in scores.items() if s >= HOT_MIN and not covered), reverse=True)
            print("rated:", ", ".join(f"{new[i]['title'][:60]}={s}" for i, (s, _) in sorted(scores.items())))
            for _, i in hot[:MAX_POSTS]:
                item = new[i]
                try:
                    post = write_post(item)
                    if post:
                        paths.append(deliver(post))
                    else:
                        print(f"skipped after checking: {item['title']}")
                except (Exception, ai.Deadline) as e:
                    ai.stop_deadline()
                    print(f"hot post failed: {type(e).__name__}")
                    ai.notify("sendMessage", chat_id=ai.ADMIN, text=FAILED.format(item["title"], ai.reason(e)))
                    break
    cutoff = (datetime.fromisoformat(stamp) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M")
    save(SEEN, {k: v for k, v in seen.items() if v >= cutoff})
    save(NOTES, notes)
    return paths


def main():
    os.makedirs(STATE, exist_ok=True)
    costs.task = "hot"
    costs.FILE = PENDING_COSTS  # one commit of costs.jsonl now and then instead of one per run
    ai.start_deadline(7)  # hot.yml stops the job at 10 minutes
    try:
        paths = check()
    finally:
        ai.stop_deadline()
    flushed = flush_costs(force=bool(paths))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"files={' '.join(paths)}\nsave={'true' if paths or flushed else ''}\n")


if __name__ == "__main__":
    main()
