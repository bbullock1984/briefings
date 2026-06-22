#!/usr/bin/env python3
"""
fetch_and_build_feed.py

Daily personalized briefing -> RSS feed.

What this does, each time it runs:
  1. Loads config.json (topics + feed settings).
  2. Loads docs/items_store.json (the running history of past briefings —
     this is also how we know what's already been covered, so we don't
     repeat the same story two days in a row).
  3. Builds a prompt asking Claude (with web search enabled) to find
     5-6 fresh stories across the configured topics.
  4. Calls the Anthropic API.
  5. Parses the JSON it returns, builds today's briefing entry, and
     folds it into items_store.json (one entry per day).
  6. Rewrites docs/feed.xml from the full (capped) history in items_store.

Run with --test to skip the live API call and use canned sample data
instead — useful for checking the RSS-building logic works before
spending API credits or pushing to GitHub Actions.
"""

import argparse
import datetime
import json
import os
import re
import sys
import xml.sax.saxutils as saxutils

import requests

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")
DOCS_DIR = os.path.join(REPO_ROOT, "docs")
ITEMS_STORE_PATH = os.path.join(DOCS_DIR, "items_store.json")
FEED_PATH = os.path.join(DOCS_DIR, "feed.xml")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"

REQUIRED_STORY_FIELDS = [
    "topic_id",
    "headline",
    "summary",
    "source_name",
    "source_url",
    "is_social",
]


# ---------------------------------------------------------------------------
# Config / state I/O
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_items_store():
    if not os.path.exists(ITEMS_STORE_PATH):
        return []
    with open(ITEMS_STORE_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            print("WARNING: items_store.json was unreadable, starting fresh.", file=sys.stderr)
            return []


def save_items_store(items_store):
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(ITEMS_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(items_store, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_topics_block(topics):
    lines = []
    for t in topics:
        lines.append(f'- topic_id "{t["id"]}" — {t["label"]}\n  Instructions: {t["brief"]}')
    return "\n".join(lines)


def build_recent_coverage_block(items_store, lookback_days):
    cutoff = (datetime.date.today() - datetime.timedelta(days=lookback_days)).isoformat()
    lines = []
    for entry in items_store:
        if entry.get("date", "") >= cutoff:
            for story in entry.get("stories", []):
                lines.append(
                    f'- [{entry["date"]}] ({story.get("topic_id", "?")}) '
                    f'{story.get("headline", "")} — {story.get("source_name", "")}'
                )
    if not lines:
        return "(no recent coverage on file yet — this may be the first run)"
    return "\n".join(lines)


def build_prompt(config, items_store):
    topics_block = build_topics_block(config["topics"])
    recent_block = build_recent_coverage_block(items_store, config.get("lookback_days", 3))
    today_str = datetime.date.today().isoformat()
    stories_per_day = config.get("stories_per_day", 6)

    return f"""You are building a personalized daily news briefing. Today's date is {today_str}.

Use web search to find current news for EACH of the topics below. Prefer
professional news sources. You may use Reddit, X/Twitter, or other social
media as a source, but if you do, you MUST set "is_social": true for that
story so it gets labeled as such for the reader.

TOPICS:
{topics_block}

Stories should generally be from the last 24 hours; a story up to 3 days
old is acceptable if it's still the most relevant news on that topic.

ALREADY COVERED RECENTLY (do not repeat these same stories/events unless
there is a genuinely new and important development — if you do include an
update to one of these, make clear in the summary what specifically is new):
{recent_block}

Return a TOTAL of {stories_per_day - 1}-{stories_per_day} stories across ALL topics combined
(not per topic). It's fine for a topic to contribute 0 stories if there's
genuinely nothing new and notable, and fine for another topic to contribute
2 if there's a lot happening — just hit the total target across all topics.

Respond with ONLY a JSON array, no markdown code fences, no commentary
before or after. Each element must be an object with exactly these fields:
  "topic_id": one of the topic_id values above
  "headline": short headline, your own words (do not just copy the source's exact headline verbatim)
  "summary": 2-4 sentence summary in your own words
  "source_name": the publication or outlet name
  "source_url": the direct URL to the source article
  "is_social": true if source_name is Reddit/X/Twitter/similar social media, else false

Example shape (values illustrative only):
[
  {{"topic_id": "soccer", "headline": "...", "summary": "...", "source_name": "...", "source_url": "https://...", "is_social": false}}
]
"""


# ---------------------------------------------------------------------------
# Anthropic API call
# ---------------------------------------------------------------------------

def call_claude(prompt):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")

    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 4096,
            "tools": [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 16}
            ],
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=240,
    )
    resp.raise_for_status()
    data = resp.json()

    text_parts = [
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    text = "\n".join(text_parts).strip()
    if not text:
        raise RuntimeError(f"No text content in API response: {json.dumps(data)[:1000]}")
    return text


def parse_stories(raw_text):
    text = raw_text.strip()
    # Strip markdown fences if the model added them despite instructions.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        stories = json.loads(text)
    except json.JSONDecodeError:
        # Last resort: try to recover a JSON array embedded in extra text.
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            raise RuntimeError(f"Could not parse JSON from model output. Raw output:\n{text[:2000]}")
        stories = json.loads(match.group(0))

    if not isinstance(stories, list):
        raise RuntimeError(f"Expected a JSON array of stories, got: {type(stories)}")

    cleaned = []
    for s in stories:
        if not isinstance(s, dict):
            continue
        if not all(field in s for field in REQUIRED_STORY_FIELDS):
            print(f"WARNING: skipping story missing required fields: {s}", file=sys.stderr)
            continue
        cleaned.append(s)

    if not cleaned:
        raise RuntimeError("No valid stories survived parsing/validation.")

    return cleaned


# ---------------------------------------------------------------------------
# RSS building
# ---------------------------------------------------------------------------

def build_item_html(stories, topics_by_id):
    grouped = {}
    for s in stories:
        grouped.setdefault(s["topic_id"], []).append(s)

    parts = []
    # Preserve the topic order from config, then append any unknown topic_ids.
    ordered_ids = list(topics_by_id.keys()) + [tid for tid in grouped if tid not in topics_by_id]
    for tid in ordered_ids:
        if tid not in grouped:
            continue
        label = topics_by_id.get(tid, {}).get("label", tid)
        parts.append(f"<h3>{saxutils.escape(label)}</h3>")
        parts.append("<ul>")
        for s in grouped[tid]:
            headline = saxutils.escape(s["headline"])
            url = saxutils.escape(s["source_url"], {"'": "&apos;"})
            summary = saxutils.escape(s["summary"])
            source = saxutils.escape(s["source_name"])
            social_tag = " <em>(social media — verify independently)</em>" if s.get("is_social") else ""
            parts.append(
                f'<li><strong><a href="{url}">{headline}</a></strong>{social_tag}<br/>'
                f"{summary}<br/>"
                f"<small>Source: {source}</small></li>"
            )
        parts.append("</ul>")
    return "\n".join(parts)


def build_item_plain(stories, topics_by_id):
    parts = []
    grouped = {}
    for s in stories:
        grouped.setdefault(s["topic_id"], []).append(s)

    ordered_ids = list(topics_by_id.keys()) + [tid for tid in grouped if tid not in topics_by_id]
    for tid in ordered_ids:
        if tid not in grouped:
            continue
        label = topics_by_id.get(tid, {}).get("label", tid)
        parts.append(label + ":")
        for s in grouped[tid]:
            parts.append(f"{s['headline']} — {s['summary']} (Source: {s['source_name']})")
    return " ".join(parts)


def rfc822_date(date_str, hour=11):
    d = datetime.date.fromisoformat(date_str)
    dt = datetime.datetime(d.year, d.month, d.day, hour, 0, 0)
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def build_rss(config, items_store, topics_by_id):
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    title = config["feed_title"]
    link = config["feed_link"]
    description = config["feed_description"]

    items_xml = []
    # Newest first.
    for entry in sorted(items_store, key=lambda e: e["date"], reverse=True):
        item_title = f"Daily Briefing — {entry['date']}"
        item_html = build_item_html(entry["stories"], topics_by_id)
        plain_fallback = build_item_plain(entry["stories"], topics_by_id)[:500]

        items_xml.append(
            "    <item>\n"
            f"      <title>{saxutils.escape(item_title)}</title>\n"
            f"      <link>{saxutils.escape(link)}</link>\n"
            f'      <guid isPermaLink="false">{saxutils.escape(entry["guid"])}</guid>\n'
            f"      <pubDate>{rfc822_date(entry['date'])}</pubDate>\n"
            f"      <description>{saxutils.escape(plain_fallback)}</description>\n"
            f"      <content:encoded><![CDATA[{item_html}]]></content:encoded>\n"
            "    </item>\n"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">\n'
        "  <channel>\n"
        f"    <title>{saxutils.escape(title)}</title>\n"
        f"    <link>{saxutils.escape(link)}</link>\n"
        f"    <description>{saxutils.escape(description)}</description>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        + "".join(items_xml)
        + "  </channel>\n"
        "</rss>\n"
    )


# ---------------------------------------------------------------------------
# Sample data for --test
# ---------------------------------------------------------------------------

def sample_stories():
    return [
        {
            "topic_id": "bjj",
            "headline": "ADCC announces 2026 trials schedule",
            "summary": "Organizers confirmed regional trial dates for the next ADCC cycle, with the North American trial set for late summer.",
            "source_name": "BJJEE",
            "source_url": "https://example.com/adcc-trials",
            "is_social": False,
        },
        {
            "topic_id": "soccer",
            "headline": "Atlético de Madrid completes move for young winger",
            "summary": "Atlético confirmed the signing of a 21-year-old winger on a five-year deal, pending league registration.",
            "source_name": "Marca",
            "source_url": "https://example.com/atletico-signing",
            "is_social": False,
        },
        {
            "topic_id": "financial-freedom",
            "headline": "AAPL ticks up on strong services revenue chatter",
            "summary": "Shares moved higher amid analyst commentary pointing to continued growth in Apple's services segment ahead of earnings.",
            "source_name": "Reuters",
            "source_url": "https://example.com/aapl-services",
            "is_social": False,
        },
        {
            "topic_id": "nj-local",
            "headline": "Montclair Film announces summer outdoor screening series",
            "summary": "The Montclair Film organization confirmed dates for free outdoor movie nights at a local park this summer, family-friendly programming included.",
            "source_name": "Montclair Local",
            "source_url": "https://example.com/montclair-film-series",
            "is_social": False,
        },
        {
            "topic_id": "nj-local",
            "headline": "NJ homeowners discuss rising utility rates ahead of summer",
            "summary": "A thread on a NJ-focused subreddit collected anecdotes about rate hikes; no official utility statement yet confirms the scale.",
            "source_name": "r/newjersey",
            "source_url": "https://example.com/reddit-thread",
            "is_social": True,
        },
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Use sample data instead of calling the API.")
    args = parser.parse_args()

    config = load_config()
    topics_by_id = {t["id"]: t for t in config["topics"]}
    items_store = load_items_store()

    today_str = datetime.date.today().isoformat()

    if args.test:
        print("Running in --test mode: using sample data, no API call.")
        stories = sample_stories()
    else:
        prompt = build_prompt(config, items_store)
        raw_text = call_claude(prompt)
        stories = parse_stories(raw_text)

    # Trim to the configured max in case the model overshoots.
    max_stories = config.get("stories_per_day", 6)
    stories = stories[:max_stories]

    new_entry = {
        "date": today_str,
        "guid": f"briefing-{today_str}",
        "stories": stories,
    }

    # Replace any existing entry for today (e.g. manual re-run), then prune.
    items_store = [e for e in items_store if e.get("date") != today_str]
    items_store.append(new_entry)
    items_store.sort(key=lambda e: e["date"], reverse=True)

    keep_days = config.get("feed_history_days", 30)
    cutoff = (datetime.date.today() - datetime.timedelta(days=keep_days)).isoformat()
    items_store = [e for e in items_store if e["date"] >= cutoff]

    save_items_store(items_store)

    rss_xml = build_rss(config, items_store, topics_by_id)
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(FEED_PATH, "w", encoding="utf-8") as f:
        f.write(rss_xml)

    print(f"Wrote {len(stories)} stories for {today_str}.")
    print(f"Feed now has {len(items_store)} day(s) of history.")
    print(f"Updated: {FEED_PATH}")


if __name__ == "__main__":
    main()
