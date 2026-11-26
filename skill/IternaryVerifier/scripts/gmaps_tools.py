#!/usr/bin/env python3
"""Keyless Google Maps & routing tools for IternaryVerifier.

Supports:
  1. Non-hotel place resolution & Google Maps reviews (relevant, recent, lowest-rated)
  2. Searching for places near a spot (place/address or lat,lng + keyword)
  3. Routing between origin and destination (driving/walking/cycling via OSRM)
  4. Searching for places along a route corridor

Uses Python standard library only; no API keys or browser required.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
import urllib.parse
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}
OSRM_PROFILES = {
    "driving": "driving",
    "walking": "foot",
    "cycling": "bike",
}
SORT_MODES = {
    "relevant": 1,
    "recent": 2,
    "highest": 3,
    "lowest": 4,
}


def fetch_text(url: str, timeout: int = 15, headers: dict[str, str] | None = None, retries: int = 2) -> tuple[str, str]:
    """Fetch URL text and return (final_url, body)."""
    merged = dict(DEFAULT_HEADERS)
    if headers:
        merged.update(headers)
    last_exc: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, headers=merged)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "ignore")
                if body:
                    return resp.geturl(), body
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    return url, ""


def parse_xssi_json(raw: str):
    """Strip Google's )]}' XSSI prefix and parse JSON."""
    if raw.startswith(")]}'"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[4:]
    return json.loads(raw)


def safe_get(obj, *indices):
    """Safely index nested lists without raising IndexError or TypeError."""
    cur = obj
    for idx in indices:
        if isinstance(cur, list) and -len(cur) <= idx < len(cur):
            cur = cur[idx]
        else:
            return None
    return cur


def data_id_to_cid(data_id: str) -> int | None:
    """Convert hex data_id (0x...:0x...) to decimal CID."""
    m = re.match(r"0x[0-9a-fA-F]+:0x([0-9a-fA-F]+)$", data_id.strip())
    return int(m.group(1), 16) if m else None


def build_maps_url(data_id: str | None = None, place_id: str | None = None) -> str | None:
    """Build a canonical Google Maps URL for a place."""
    if place_id:
        return f"https://www.google.com/maps/place/?q=place_id:{place_id}"
    if data_id:
        cid = data_id_to_cid(data_id)
        if cid is not None:
            return f"https://www.google.com/maps?cid={cid}"
    return None


def extract_data_id_from_url(url: str) -> str | None:
    """Extract 0x...:0x... data_id from a Google Maps URL (following short links if needed)."""
    target = url.strip()
    if any(host in target for host in ("maps.app.goo.gl", "goo.gl/maps")):
        try:
            target, _ = fetch_text(target, timeout=12)
        except Exception:
            pass

    decoded = urllib.parse.unquote(target)
    m_hex = re.search(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", decoded)
    if m_hex:
        return m_hex.group(0)

    m_cid = re.search(r"[?&]cid=(\d+)", decoded)
    if m_cid:
        return f"0x0:0x{int(m_cid.group(1)):x}"

    m_pid = re.search(r"(?:place_id:|query_place_id=)(ChIJ[A-Za-z0-9_-]+)", decoded)
    if m_pid:
        try:
            _, pid_html = fetch_text(f"https://www.google.com/maps/place/?q=place_id:{m_pid.group(1)}", timeout=12)
            m_pid_hex = re.search(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", pid_html)
            if m_pid_hex:
                return m_pid_hex.group(0)
        except Exception:
            pass

    # Fallback: extract /maps/place/<name>/ and resolve via search_places
    m_name = re.search(r"/maps/place/([^/@]+)", decoded)
    if m_name:
        name = m_name.group(1).replace("+", " ")
        hits = search_places(name, limit=1)
        if hits and hits[0].get("data_id"):
            return hits[0]["data_id"]
    return None


def _parse_tbm_map_place(item: list) -> dict | None:
    p = safe_get(item, 14)
    if not isinstance(p, list) or len(p) < 12:
        return None
    name = safe_get(p, 11)
    data_id = safe_get(p, 10)
    place_id = safe_get(p, 78)
    lat = safe_get(p, 9, 2)
    lng = safe_get(p, 9, 3)
    rating = safe_get(p, 4, 7)
    review_count = safe_get(p, 4, 8)
    address = safe_get(p, 39) or safe_get(p, 18)
    category = safe_get(p, 13, 0)
    if not name and not data_id:
        return None
    return {
        "name": name,
        "data_id": data_id,
        "place_id": place_id,
        "rating": rating,
        "review_count": review_count,
        "category": category,
        "address": address,
        "lat": lat,
        "lng": lng,
        "maps_url": build_maps_url(data_id, place_id),
    }


def search_places(
    query: str,
    lat: float | None = None,
    lng: float | None = None,
    radius_m: int = 5000,
    limit: int = 10,
    hl: str = "en",
    gl: str = "my",
) -> list[dict]:
    """Search Google Maps (search?tbm=map) for places matching query, optionally near lat/lng."""
    params = {
        "tbm": "map",
        "authuser": "0",
        "hl": hl,
        "gl": gl,
        "q": query,
    }
    url = "https://www.google.com/search?" + urllib.parse.urlencode(params)
    if lat is not None and lng is not None:
        fetch_count = max(1, min(limit, 20))
        pb = f"!4m8!1m3!1d{int(radius_m)}!2d{lng}!3d{lat}!3m2!1i1024!2i768!4f13.1!7i{fetch_count}!10b1"
        url += f"&pb={pb}"

    _, raw = fetch_text(url, timeout=15)
    data = parse_xssi_json(raw)
    items = safe_get(data, 0, 1) or []
    results: list[dict] = []
    seen: set[str] = set()
    for item in items:
        parsed = _parse_tbm_map_place(item)
        if not parsed:
            continue
        key = parsed.get("data_id") or f"{parsed.get('name')}|{parsed.get('address')}"
        if key in seen:
            continue
        seen.add(key)
        results.append(parsed)
        if len(results) >= limit:
            break
    return results


def resolve_location(location: str) -> dict:
    """Resolve 'lat,lng' or a place/address string to {name, lat, lng, data_id, address}."""
    m_coord = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$", location)
    if m_coord:
        lat, lng = float(m_coord.group(1)), float(m_coord.group(2))
        return {"name": f"{lat},{lng}", "lat": lat, "lng": lng, "data_id": None, "address": None}

    hits = search_places(location, limit=1)
    if hits and hits[0].get("lat") is not None and hits[0].get("lng") is not None:
        h = hits[0]
        return {
            "name": h.get("name") or location,
            "lat": h["lat"],
            "lng": h["lng"],
            "data_id": h.get("data_id"),
            "address": h.get("address"),
        }

    # Nominatim fallback for generic towns/roads if tbm=map yields no coordinates
    nom_url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": location, "format": "json", "limit": 1}
    )
    _, raw = fetch_text(nom_url, timeout=15, headers={"User-Agent": "IternaryVerifier/1.0"})
    nom = json.loads(raw)
    if nom:
        return {
            "name": nom[0].get("display_name", location),
            "lat": float(nom[0]["lat"]),
            "lng": float(nom[0]["lon"]),
            "data_id": None,
            "address": nom[0].get("display_name"),
        }
    raise ValueError(f"Could not resolve location: {location}")


def _extract_preview_link(data_id: str, hl: str = "en") -> str:
    place_url = f"https://www.google.com/maps/place/x/data=!4m2!3m1!1s{data_id}?hl={urllib.parse.quote(hl)}"
    _, raw_html = fetch_text(place_url, timeout=15)
    m = re.search(r'href="(/maps/preview/place[^"]+)"', raw_html)
    if not m:
        raise ValueError(f"Could not find /maps/preview/place link for data_id={data_id}")
    return "https://www.google.com" + html.unescape(m.group(1))


def _parse_preview_reviews(payload: list, limit: int = 5) -> list[dict]:
    items = safe_get(payload, 6, 175, 9, 0, 0) or []
    reviews: list[dict] = []
    for entry in items:
        rv = safe_get(entry, 0)
        if not isinstance(rv, list):
            continue
        rating = safe_get(rv, 2, 0, 0)
        date_str = safe_get(rv, 1, 6)
        author = safe_get(rv, 1, 4, 5, 0)
        text = safe_get(rv, 2, 15, 0, 0) or ""
        if text or rating is not None:
            reviews.append({
                "rating": rating,
                "date": date_str,
                "author": author,
                "text": text.strip(),
            })
        if len(reviews) >= limit:
            break
    return reviews


def fetch_maps_place_reviews(data_id: str, limit: int = 5, hl: str = "en") -> dict:
    """Fetch Google Maps place metadata and relevant/recent/lowest reviews without an API key."""
    base_link = _extract_preview_link(data_id, hl=hl)

    metadata: dict = {}
    reviews_by_sort: dict[str, list[dict]] = {}

    for label, sort_val in (("relevant", 1), ("recent", 2), ("lowest", 4)):
        if "%216m1%211e1%219b1" in base_link:
            sort_link = base_link.replace("%216m1%211e1%219b1", f"%216m1%211e{sort_val}%219b1")
        else:
            sort_link = base_link
        _, raw = fetch_text(sort_link, timeout=15)
        payload = parse_xssi_json(raw)
        if not metadata:
            p = safe_get(payload, 6) or []
            hist = safe_get(p, 175, 3)
            topics_raw = safe_get(p, 153) or []
            topics = {}
            for t in topics_raw:
                t_name = safe_get(t, 1)
                t_count = safe_get(t, 3, 4)
                if isinstance(t_name, str) and t_name:
                    topics[t_name] = {"mentions": t_count}
            metadata = {
                "title": safe_get(p, 11),
                "source_url": build_maps_url(data_id=data_id, place_id=safe_get(p, 78)),
                "data_id": data_id,
                "address": safe_get(p, 39) or safe_get(p, 18),
                "google_rating": safe_get(p, 4, 7),
                "google_review_count": safe_get(p, 4, 8),
                "rating_histogram": (
                    {"1": hist[0], "2": hist[1], "3": hist[2], "4": hist[3], "5": hist[4]}
                    if isinstance(hist, list) and len(hist) == 5
                    else None
                ),
                "topics": topics,
            }
        reviews_by_sort[label] = _parse_preview_reviews(payload, limit=limit)

    return {
        **metadata,
        "relevant_reviews": reviews_by_sort.get("relevant", []),
        "recent_reviews": reviews_by_sort.get("recent", []),
        "lowest_rated_reviews": reviews_by_sort.get("lowest", []),
    }


def _format_duration(seconds: float) -> str:
    mins = round(seconds / 60)
    if mins < 60:
        return f"{mins} min"
    hours, rem = divmod(mins, 60)
    return f"{hours} hr {rem} min" if rem else f"{hours} hr"


def route_between(origin: str, destination: str, mode: str = "driving", include_geometry: bool = False) -> dict:
    """Compute route distance, estimated duration, and optional coordinates via OSRM."""
    profile = OSRM_PROFILES.get(mode)
    if not profile:
        raise ValueError(f"Unsupported mode '{mode}'. Choose from: {', '.join(OSRM_PROFILES)}")

    orig = resolve_location(origin)
    dest = resolve_location(destination)

    osrm_url = (
        f"https://router.project-osrm.org/route/v1/{profile}/"
        f"{orig['lng']},{orig['lat']};{dest['lng']},{dest['lat']}"
        "?overview=full&geometries=geojson"
    )
    _, raw = fetch_text(osrm_url, timeout=15, headers={"User-Agent": "IternaryVerifier/1.0"})
    data = json.loads(raw)
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError(f"OSRM routing failed: {data.get('message') or data.get('code')}")

    route = data["routes"][0]
    dist_m = round(route["distance"])
    dur_s = round(route["duration"])
    gmaps_dir = (
        "https://www.google.com/maps/dir/?api=1&"
        + urllib.parse.urlencode({
            "origin": f"{orig['lat']},{orig['lng']}",
            "destination": f"{dest['lat']},{dest['lng']}",
            "travelmode": mode,
        })
    )
    out = {
        "origin": orig,
        "destination": dest,
        "mode": mode,
        "distance_m": dist_m,
        "distance_km": round(dist_m / 1000, 2),
        "duration_s": dur_s,
        "duration_min": round(dur_s / 60, 1),
        "duration_text": _format_duration(dur_s),
        "google_maps_dir_url": gmaps_dir,
    }
    if include_geometry:
        # OSRM returns [lng, lat] pairs
        out["geometry"] = route["geometry"]["coordinates"]
    return out


def _sample_route_points(coords: list[list[float]], samples: int) -> list[tuple[float, float]]:
    """Sample evenly spaced (lat, lng) points along a GeoJSON [lng, lat] coordinate list."""
    if not coords:
        return []
    if samples <= 1 or len(coords) == 1:
        mid = coords[len(coords) // 2]
        return [(mid[1], mid[0])]
    # Compute cumulative Euclidean/haversine arc length to sample evenly by distance
    cum = [0.0]
    for i in range(1, len(coords)):
        dx = coords[i][0] - coords[i - 1][0]
        dy = coords[i][1] - coords[i - 1][1]
        cum.append(cum[-1] + math.hypot(dx, dy))
    total = cum[-1]
    if total == 0:
        return [(coords[0][1], coords[0][0])]

    points: list[tuple[float, float]] = []
    idx = 0
    for s in range(samples):
        target = total * (s / (samples - 1))
        while idx < len(cum) - 2 and cum[idx + 1] < target:
            idx += 1
        lng, lat = coords[idx]
        points.append((round(lat, 6), round(lng, 6)))
    return points


def search_along_route(
    origin: str,
    destination: str,
    query: str,
    samples: int = 5,
    radius_m: int = 5000,
    limit: int = 15,
    mode: str = "driving",
) -> dict:
    """Sample points along a route and search Google Maps near each point."""
    route = route_between(origin, destination, mode=mode, include_geometry=True)
    coords = route.pop("geometry", [])
    sample_pts = _sample_route_points(coords, max(2, min(samples, 10)))

    per_point = max(2, math.ceil(limit / len(sample_pts)) + 1)
    buckets: list[list[dict]] = []
    for lat, lng in sample_pts:
        hits = search_places(query, lat=lat, lng=lng, radius_m=radius_m, limit=per_point)
        for h in hits:
            h["sampled_near"] = {"lat": lat, "lng": lng}
        buckets.append(hits)

    # Interleave across sample points so results span the entire route corridor
    merged: list[dict] = []
    seen: set[str] = set()
    max_len = max((len(b) for b in buckets), default=0)
    for idx in range(max_len):
        for b in buckets:
            if idx >= len(b):
                continue
            h = b[idx]
            key = h.get("data_id") or f"{h.get('name')}|{h.get('address')}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(h)
            if len(merged) >= limit:
                break
        if len(merged) >= limit:
            break

    return {
        "query": query,
        "route": route,
        "sample_points": [{"lat": lat, "lng": lng} for lat, lng in sample_pts],
        "count": len(merged[:limit]),
        "results": merged[:limit],
    }


def is_google_hotels_url(url: str) -> bool:
    return "/travel/hotels/entity/" in url


def normalize_hotel_reviews_url(url: str) -> str:
    base = url.split("?")[0].rstrip("/")
    if not base.endswith("/reviews"):
        base += "/reviews"
    return base


def normalize_hotel_prices_url(url: str) -> str:
    base = url.split("?")[0].rstrip("/")
    if base.endswith("/reviews"):
        base = base[: -len("/reviews")]
    if not base.endswith("/prices"):
        base += "/prices"
    return base + "?hl=en"


def find_google_hotel_url(query: str) -> str | None:
    ddg_url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(
        f"site:google.com/travel/hotels/entity {query}"
    )
    try:
        _, raw = fetch_text(ddg_url, timeout=12)
    except Exception:
        return None
    for m in re.finditer(r"https?://www\.google\.com(?:\.[a-z]+)?/travel/hotels/entity/[A-Za-z0-9_-]+", raw):
        return m.group(0)
    for m in re.finditer(r"uddg=([^&\"']+)", raw):
        decoded = urllib.parse.unquote(m.group(1))
        hit = re.search(r"https?://www\.google\.com(?:\.[a-z]+)?/travel/hotels/entity/[A-Za-z0-9_-]+", decoded)
        if hit:
            return hit.group(0)
    return None


def fetch_google_travel_reviews(raw_html: str, source_url: str, sort: int = 4, limit: int = 10) -> list[dict]:
    """Fetch individual Google Hotel reviews via TravelFrontendUi ocp93e RPC (1=relevant, 2=recent, 3=highest, 4=lowest)."""
    m_fsid = re.search(r'"FdrFJe":"([^"]+)"', raw_html)
    m_bl = re.search(r'"cfb2h":"([^"]+)"', raw_html)
    m_ent = re.search(r"/travel/hotels/entity/([A-Za-z0-9_-]+)", source_url)
    if not (m_fsid and m_bl and m_ent):
        return []

    entity = m_ent.group(1)
    fsid = m_fsid.group(1)
    bl = m_bl.group(1)
    ep = (
        "https://www.google.com/_/TravelFrontendUi/data/batchexecute"
        f"?rpcids=ocp93e&source-path=%2Ftravel%2Fhotels%2Fentity%2F{entity}%2Freviews"
        f"&f.sid={urllib.parse.quote(fsid)}&bl={urllib.parse.quote(bl)}&hl=en&gl=my&_reqid=123456&rt=c"
    )
    payload = [None, None, None, limit, sort, None, None, "", entity, "", None, [[]], None, ""]
    f_req = json.dumps([[["ocp93e", json.dumps(payload), None, "generic"]]])
    body = urllib.parse.urlencode({"f.req": f_req}).encode()
    req = urllib.request.Request(
        ep,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
    except Exception:
        return []

    reviews: list[dict] = []
    for line in resp.splitlines():
        if '"wrb.fr"' not in line:
            continue
        try:
            outer = json.loads(line)
            inner = json.loads(outer[0][2])
            items = inner[0][0]
        except Exception:
            continue
        for item in items:
            try:
                b = item[1]
                author = b[0][0] if isinstance(b[0], list) and b[0] else None
                date_str = b[1] if len(b) > 1 else None
                rating = b[2][0] if len(b) > 2 and isinstance(b[2], list) else None
                raw_txt = ""
                if len(b) > 3 and isinstance(b[3], list) and b[3] and isinstance(b[3][0], list):
                    for seg in b[3][0]:
                        if isinstance(seg, list) and len(seg) > 1 and isinstance(seg[1], str):
                            raw_txt = seg[1]
                            break
                clean_txt = html.unescape(re.sub(r"<br\s*/?>", "\n", raw_txt)).strip()
                if clean_txt or rating is not None:
                    reviews.append({"rating": rating, "date": date_str, "author": author, "text": clean_txt})
            except Exception:
                continue
        break
    return reviews


def parse_google_travel_prices(prices_url: str) -> dict:
    """Fetch and parse live nightly prices and OTA offers from Google Hotels /prices."""
    try:
        _, raw_html = fetch_text(prices_url)
    except Exception as exc:
        return {"source_url": prices_url, "error": str(exc), "offers": []}

    text = re.sub(r"<script[^>]*>.*?</script>", " ", raw_html, flags=re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", text))
    lines = [line.strip().replace("\xa0", " ") for line in text.splitlines() if line.strip()]

    typical_range = None
    for line in lines:
        if "typically costs between" in line.lower():
            typical_range = line
            break

    offers: list[dict] = []
    seen_providers: set[str] = set()
    for i, line in enumerate(lines):
        if line != "Stay total with taxes + fees":
            continue
        price_candidates = [
            w for w in lines[i + 1 : min(len(lines), i + 6)]
            if re.match(r"^(?:[A-Z]{2,3}|[A-Z]{0,2}\$|¥|€|£)\s?[\d,]+(?:\.\d+)?$", w)
        ]
        if not price_candidates:
            continue
        nightly_with_taxes = price_candidates[-2] if len(price_candidates) >= 2 else price_candidates[0]
        stay_total = price_candidates[-1]

        provider = None
        cancellation = None
        for j in range(i - 1, max(-1, i - 10), -1):
            w = lines[j]
            if w in {"Total per night", "Nightly price with taxes + fees", "·", ",", "Prices", "Visit site"}:
                continue
            if "cancellation" in w.lower() or "refundable" in w.lower():
                cancellation = w
                continue
            provider = w
            break
        if provider and provider not in seen_providers:
            seen_providers.add(provider)
            offers.append({
                "provider": provider,
                "nightly_with_taxes": nightly_with_taxes,
                "stay_total_with_taxes": stay_total,
                "cancellation": cancellation,
            })

    return {
        "source_url": prices_url,
        "typical_range": typical_range,
        "lowest_offer": offers[0] if offers else None,
        "offers": offers[:10],
    }


def fetch_hotel_reviews_and_prices(hotel_url: str, limit: int = 10) -> dict:
    """Fetch Google Hotels summary, realistic/recent/lowest reviews, and live prices."""
    reviews_url = normalize_hotel_reviews_url(hotel_url)
    _, raw_html = fetch_text(reviews_url)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", raw_html, flags=re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", text))
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    title = lines[0].replace(" - Google hotels", "").strip() if lines else ""
    rating = None
    review_count = None
    topics: dict[str, dict] = {}

    for i, line in enumerate(lines):
        if line == "Google review summary" and i + 1 < len(lines):
            for w in lines[i : min(len(lines), i + 15)]:
                if rating is None and re.fullmatch(r"[1-5](?:\.\d)?", w):
                    rating = float(w)
                m_cnt = re.match(r"([\d,]+)\s+reviews?$", w, re.I)
                if review_count is None and m_cnt:
                    review_count = int(m_cnt.group(1).replace(",", ""))
        m_topic = re.match(r"^Mentioned in (\d+) Google reviews?$", line, re.I)
        if m_topic and i > 0:
            topic_name = lines[i - 1]
            mentions = int(m_topic.group(1))
            unfav = None
            pos = None
            for w in lines[i + 1 : min(len(lines), i + 5)]:
                m_u = re.match(r"(\d+)%\s+unfavorable", w, re.I)
                m_p = re.match(r"(\d+)%\s+positive", w, re.I)
                if m_u:
                    unfav = int(m_u.group(1))
                elif m_p:
                    pos = int(m_p.group(1))
            topics[topic_name] = {"mentions": mentions, "unfavorable_pct": unfav, "positive_pct": pos}

    return {
        "title": title,
        "source_url": reviews_url,
        "google_rating": rating,
        "google_review_count": review_count,
        "prices": parse_google_travel_prices(normalize_hotel_prices_url(reviews_url)),
        "topics": topics,
        "relevant_reviews": fetch_google_travel_reviews(raw_html, reviews_url, sort=1, limit=limit),
        "recent_reviews": fetch_google_travel_reviews(raw_html, reviews_url, sort=2, limit=limit),
        "lowest_rated_reviews": fetch_google_travel_reviews(raw_html, reviews_url, sort=4, limit=limit),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Keyless Google Maps & routing tools for IternaryVerifier.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_near = sub.add_parser("near", help="Search for places near a spot or lat,lng.")
    p_near.add_argument("--near", required=True, help="Place name/address or 'lat,lng'")
    p_near.add_argument("--query", required=True, help="Search keyword (e.g. 'cafe', 'snooker', 'petrol')")
    p_near.add_argument("--radius", type=int, default=5000, help="Search radius in meters (default 5000)")
    p_near.add_argument("--limit", type=int, default=10, help="Max results (default 10)")

    p_route = sub.add_parser("route", help="Route between origin and destination.")
    p_route.add_argument("--origin", required=True, help="Origin place/address or 'lat,lng'")
    p_route.add_argument("--destination", required=True, help="Destination place/address or 'lat,lng'")
    p_route.add_argument("--mode", choices=sorted(OSRM_PROFILES), default="driving", help="Travel mode")

    p_along = sub.add_parser("along-route", help="Search for places along a route corridor.")
    p_along.add_argument("--origin", required=True, help="Origin place/address or 'lat,lng'")
    p_along.add_argument("--destination", required=True, help="Destination place/address or 'lat,lng'")
    p_along.add_argument("--query", required=True, help="Search keyword")
    p_along.add_argument("--samples", type=int, default=5, help="Route sample points (default 5)")
    p_along.add_argument("--radius", type=int, default=5000, help="Search radius per point in meters (default 5000)")
    p_along.add_argument("--limit", type=int, default=15, help="Max merged results (default 15)")
    p_along.add_argument("--mode", choices=sorted(OSRM_PROFILES), default="driving", help="Travel mode")

    p_rev = sub.add_parser("reviews", help="Fetch Google reviews (hotels + prices, or non-hotel Google Maps places).")
    p_rev.add_argument("--query", help="Place or hotel name + city")
    p_rev.add_argument("--url", help="Google Hotels entity URL or Google Maps place URL / short link")
    p_rev.add_argument("--data-id", help="Google Maps 0x...:0x... data_id / ftid")
    p_rev.add_argument("--maps", action="store_true", help="Skip Google Hotels lookup and resolve as Google Maps place")
    p_rev.add_argument("--limit", type=int, default=5, help="Max reviews per sort bucket (default 5)")

    args = parser.parse_args()
    try:
        if args.command == "near":
            anchor = resolve_location(args.near)
            results = search_places(
                args.query,
                lat=anchor["lat"],
                lng=anchor["lng"],
                radius_m=args.radius,
                limit=args.limit,
            )
            out = {"anchor": anchor, "query": args.query, "count": len(results), "results": results}
        elif args.command == "route":
            out = route_between(args.origin, args.destination, mode=args.mode)
        elif args.command == "along-route":
            out = search_along_route(
                args.origin,
                args.destination,
                args.query,
                samples=args.samples,
                radius_m=args.radius,
                limit=args.limit,
                mode=args.mode,
            )
        elif args.command == "reviews":
            if args.url and is_google_hotels_url(args.url):
                out = fetch_hotel_reviews_and_prices(args.url, limit=args.limit)
            else:
                data_id = args.data_id
                if not data_id and args.url:
                    data_id = extract_data_id_from_url(args.url)
                if not data_id and args.query and not args.maps:
                    hotel_url = find_google_hotel_url(args.query)
                    if hotel_url and is_google_hotels_url(hotel_url):
                        out = fetch_hotel_reviews_and_prices(hotel_url, limit=args.limit)
                        print(json.dumps(out, indent=2, ensure_ascii=False))
                        return 0
                if not data_id and args.query:
                    hits = search_places(args.query, limit=1)
                    data_id = hits[0].get("data_id") if hits else None
                if not data_id:
                    raise ValueError("Could not resolve place. Provide --data-id, --url, or --query.")
                out = fetch_maps_place_reviews(data_id, limit=args.limit)
        else:
            parser.error("Unknown command")
            return 2
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
