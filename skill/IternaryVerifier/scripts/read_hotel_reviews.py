"""Read a batch of full Google Hotels reviews without a browser or API key."""

import argparse
import html
import json
import re
import sys
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


ORIGIN = "https://www.google.com"
REVIEW_RPC = "ocp93e"
SORTS = {"bad": 4, "good": 3, "relevant": 1, "recent": 2}
MAX_BATCH = 20
TOPICS = {
    "sleep": ("Sleep", 2, "Sleep"),
    "room": ("Room amenities", None, "Room"),
    "bathroom": ("Bathroom and toiletries", 2, "Bathroom"),
    "cleanliness": ("Cleanliness", None, "Cleanliness"),
    "service": ("Service", 1, "Service"),
    "property": ("Property", 1, "Property"),
    "location": ("Location", 1, "Location"),
    "atmosphere": ("Atmosphere", None, "Atmosphere"),
}


def hotel_entity(url):
    parts = urlsplit(url)
    match = re.match(r"/travel/hotels/(?:[^/]+/)?entity/([A-Za-z0-9_-]+)(?:/.*)?$", parts.path)
    if parts.scheme != "https" or parts.netloc not in {"google.com", "www.google.com"} or not match:
        raise argparse.ArgumentTypeError("Use a Google Hotels entity URL (https://www.google.com/travel/hotels/entity/…).")
    return match[1]


def fetch_review_cards(entity, sort, topic, limit):
    # Positional RPC fields from the public Reviews tab: batch size, sort, hotel, topic.
    args = [None, None, None, limit, sort, None, None, "", entity, "", None, [topic or []], None, ""]
    data = urlencode({"f.req": json.dumps([[[REVIEW_RPC, json.dumps(args), None, "generic"]]])}).encode()
    request = Request(f"{ORIGIN}/_/TravelFrontendUi/data/batchexecute?hl=en", data=data)
    with urlopen(request, timeout=15) as response:
        raw = response.read().decode()
    if raw.startswith(")]}'\n"):
        for line in raw.split("\n"):
            if not line.startswith("["):
                continue
            for frame in json.loads(line):
                if frame[:2] == ["wrb.fr", REVIEW_RPC] and isinstance(frame[2], str):
                    page = json.loads(frame[2])[0]
                    return page[0] or [], bool(len(page) > 5 and page[5])
    raise ValueError("Google did not return a review batch")


def plain_text(text):
    # The full-text field uses line breaks and may bold the matched topic.
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    return html.unescape(re.sub(r"</?b>", "", text, flags=re.IGNORECASE)).strip()

def read_reviews(cards, filter_name):
    reviews = []
    for provider, review, *_ in cards:
        author, date, score, text_fields = review[:4]
        if not score or not score[1] or not text_fields:
            continue
        rating = 5 * score[0] / score[1]
        if (filter_name == "bad" and rating > 2) or (filter_name == "good" and rating < 4):
            continue
        block = next((part for part in (text_fields[0] or []) if part[0] == 1 and part[1]), None)
        if block is None:
            continue
        text = plain_text(block[1])
        if text:
            reviews.append({
                "author": author[0] if author else None,
                "rating": rating,
                "score": f"{score[0]}/{score[1]}",
                "date": date,
                "source": provider[0],
                "text": text,
            })
    return reviews


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", type=hotel_entity, metavar="URL", help="Google Hotels entity or reviews URL")
    parser.add_argument("--filter", choices=SORTS, default="bad", help="bad: 1–2★; good: 4–5★; relevant: most helpful; recent: newest (default: bad)")
    parser.add_argument("--topic", type=str.lower, choices=TOPICS, help="Google Hotels topic to read")
    parser.add_argument("--limit", type=int, default=10, metavar=f"1–{MAX_BATCH}", help="Maximum cards to request (default: 10)")
    args = parser.parse_args()
    if not 1 <= args.limit <= MAX_BATCH:
        parser.error(f"--limit must be between 1 and {MAX_BATCH}")

    try:
        cards, more = fetch_review_cards(args.url, SORTS[args.filter], TOPICS.get(args.topic), args.limit)
        reviews = read_reviews(cards, args.filter)
    except (OSError, ValueError, IndexError, TypeError) as exc:
        print(f"Could not read public Google Hotels reviews: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "source_url": f"{ORIGIN}/travel/hotels/entity/{args.url}/reviews?hl=en",
        "filter": args.filter,
        "topic": args.topic,
        "cards_checked": len(cards),
        "more_on_google_hotels": more,
        "reviews": reviews,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
