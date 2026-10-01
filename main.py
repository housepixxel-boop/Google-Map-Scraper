import os
import re
import csv
import io
import json
import time
import uuid
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

import requests
import certifi
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from serpapi import GoogleSearch


SERPAPI_API_KEY = os.environ.get("SERPAPI_API_KEY", "").strip()
if not SERPAPI_API_KEY:
    print("[warn] SERPAPI_API_KEY env var not set")
    print("[warn] get a key at https://serpapi.com/manage-api-key")
    print("[warn] set it via:  set SERPAPI_API_KEY=your_key_here   (Windows)")
    print("[warn]         or:  export SERPAPI_API_KEY=your_key_here  (Linux/Mac)")


GEOCODE_URL = "https://nominatim.openstreetmap.org/search"

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Place Finder API (SerpApi)", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchRequest(BaseModel):
    query: str
    location: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    zoom: int = 14
    max_results: int = 0
    fetch_details: bool = True
    max_detail_fetches: int = 0


class PlaceResult(BaseModel):
    name: str
    category: str
    rating: str
    reviews: str
    phone: str
    website: str
    address: str
    lat: str
    lon: str
    place_id: str


class SearchResponse(BaseModel):
    request_id: str
    total_results: int
    with_rating: int
    with_phone: int
    with_website: int
    with_address: int
    elapsed_seconds: float
    diagnostics: dict
    results: List[PlaceResult]


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def _geocode_location(location_text):
    if not location_text:
        return None, None
    try:
        resp = requests.get(
            GEOCODE_URL,
            params={"q": location_text, "format": "json", "limit": 1, "accept-language": "en"},
            headers={"User-Agent": "place-finder-serpapi/1.0"},
            timeout=8,
            verify=certifi.where(),
        )
        if resp.status_code == 200:
            data = resp.json()
            if data:
                return float(data[0].get("lat", 0)), float(data[0].get("lon", 0))
    except Exception:
        pass
    return None, None


def _is_junk_url(url):
    if not url:
        return True
    u = url.lower()
    junk_substrings = [
        "google.com/maps", "maps.app.goo.gl", "goo.gl/maps",
        "google.com/search", "google.com/url", "google.com/local",
        "facebook.com/l.php", "facebook.com/flx", "youtube.com/results",
        "linkedin.com/search", "instagram.com/explore", "twitter.com/search",
        "duckduckgo.com", "bing.com/search",
    ]
    for s in junk_substrings:
        if s in u:
            return True
    if u.startswith("mailto:") or u.startswith("tel:") or u.startswith("javascript:"):
        return True

    aggregator_domains = [
        "skoolyst.com", "dailywatchupdates.com", "bolt.host",
        "findglocal.com", "guidelive.com", "hischools.pk", "schools.com.pk",
        "schoolsdirectory.com", "eduhub.pk", "ilmkidunya.com", "hamariweb.com",
        "yelp.com", "tripadvisor.com", "foursquare.com", "justdial.com",
        "yellowpages.com", "sulekha.com", "urbanspoon.com", "zomato.com",
        "facebook.com/profile.php", "facebook.com/people", "facebook.com/pg/",
        "linkedin.com/company", "linkedin.com/school",
        "wikipedia.org", "wikimapia.org",
        "maps.google.com", "google.com",
        "yell.com", "hotfrog.com", "opendi.com", "brownbook.net",
        "cylex.com", "bizhq.com", "businessdirectory.com",
    ]
    try:
        from urllib.parse import urlparse as _urlparse
        host = (_urlparse(u).netloc or "").lstrip("www.")
    except Exception:
        host = ""
    for d in aggregator_domains:
        if host == d or host.endswith("." + d):
            return True
    return False


def _strip_junk_urls(rows):
    if not rows:
        return rows
    from collections import Counter
    url_counts = Counter()
    for r in rows:
        u = (r.get("website") or "").strip().lower()
        if u:
            url_counts[u] += 1
    junk_dynamic = {u for u, c in url_counts.items() if c >= 3 and u}
    cleaned = []
    for r in rows:
        u = (r.get("website") or "").strip()
        if u and (u.lower() in junk_dynamic):
            r = dict(r)
            r["website"] = ""
        cleaned.append(r)
    return cleaned


def _normalize_name(name):
    n = (name or "").lower().strip()
    n = re.sub(r"[^a-z0-9]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _dedupe(rows):
    by_id = {}
    by_name_phone = {}
    by_name_addr = {}
    by_name_geo = {}
    merged = []
    for r in rows:
        url = (r.get("website") or "").strip().lower()
        sid = (r.get("place_id") or "").strip()
        name_n = _normalize_name(r.get("name", ""))
        phone = (r.get("phone") or "").strip()
        addr_n = _normalize_name(r.get("address", ""))
        try:
            lat_k = round(float(r.get("lat") or 0), 4)
            lon_k = round(float(r.get("lon") or 0), 4)
        except (TypeError, ValueError):
            lat_k = None
            lon_k = None

        existing = None
        if sid and sid in by_id:
            existing = by_id[sid]
        elif name_n and phone and (name_n, phone) in by_name_phone:
            existing = by_name_phone[(name_n, phone)]
        elif name_n and addr_n and (name_n, addr_n) in by_name_addr:
            existing = by_name_addr[(name_n, addr_n)]
        elif name_n and lat_k is not None and lon_k is not None and (name_n, lat_k, lon_k) in by_name_geo:
            existing = by_name_geo[(name_n, lat_k, lon_k)]

        if existing is not None:
            for k in ("phone", "website", "address", "rating", "reviews", "category"):
                if not existing.get(k) and r.get(k):
                    existing[k] = r[k]
            continue

        merged.append(r)
        if sid:
            by_id[sid] = r
        if name_n and phone:
            by_name_phone[(name_n, phone)] = r
        if name_n and addr_n:
            by_name_addr[(name_n, addr_n)] = r
        if name_n and lat_k is not None and lon_k is not None:
            by_name_geo[(name_n, lat_k, lon_k)] = r
    return merged


def _quality_score(r):
    score = 0
    if r.get("phone"):
        score += 3
    if r.get("website"):
        score += 3
    if r.get("rating"):
        score += 2
    if r.get("reviews"):
        try:
            score += min(int(re.sub(r"[^\d]", "", str(r["reviews"]))) / 100, 2)
        except Exception:
            pass
    if r.get("address"):
        score += 1
    if r.get("name") and len(r["name"]) > 5:
        score += 1
    return score


def _serpapi_search(query, location, lat, lon, zoom, max_pages=3):
    params = {
        "engine": "google_maps",
        "q": f"{query} in {location}",
        "type": "search",
        "api_key": SERPAPI_API_KEY,
    }
    if lat is not None and lon is not None:
        params["ll"] = f"@{lat},{lon},{zoom}z"

    all_results = []
    diagnostics = {
        "pages_fetched": 0,
        "serpapi_status": None,
        "errors": [],
        "raw_total": 0,
    }

    for page in range(1, max_pages + 1):
        if page > 1:
            params["start"] = (page - 1) * 20
        try:
            search = GoogleSearch(params)
            data = search.get_dict()
            diagnostics["pages_fetched"] += 1
            diagnostics["serpapi_status"] = data.get("search_metadata", {}).get("status")
            local_results = data.get("local_results") or []
            if not local_results:
                break
            all_results.extend(local_results)
            diagnostics["raw_total"] = len(all_results)
            serpapi_pagination = data.get("serpapi_pagination") or {}
            next_link = serpapi_pagination.get("next") or data.get("pagination", {}).get("next")
            if not next_link:
                break
        except Exception as e:
            diagnostics["errors"].append(f"page {page}: {e}")
            break

    return all_results, diagnostics


def _serpapi_place_details(place_id, data_id=None):
    params = {
        "engine": "google_maps",
        "type": "place",
        "place_id": place_id,
        "api_key": SERPAPI_API_KEY,
    }
    if data_id:
        params["data_id"] = data_id
    try:
        search = GoogleSearch(params)
        data = search.get_dict()
        return data.get("place_results") or {}
    except Exception:
        return {}


def _normalize_row(item):
    gps = item.get("gps_coordinates") or {}
    return {
        "name": _clean(item.get("title", "")),
        "category": _clean(item.get("type", "")),
        "rating": str(item.get("rating", "")) if item.get("rating") else "",
        "reviews": str(item.get("reviews", "")) if item.get("reviews") else "",
        "phone": _clean(item.get("phone", "")),
        "website": _clean(item.get("website", "")) if not _is_junk_url(item.get("website", "")) else "",
        "address": _clean(item.get("address", "")),
        "lat": str(gps.get("latitude", "")) if gps else "",
        "lon": str(gps.get("longitude", "")) if gps else "",
        "place_id": _clean(item.get("place_id", "") or item.get("data_id", "")),
    }


@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest):
    if not req.query.strip() or not req.location.strip():
        raise HTTPException(status_code=400, detail="query and location are required")
    if not SERPAPI_API_KEY:
        raise HTTPException(status_code=500, detail="SERPAPI_API_KEY env var not set")

    request_id = str(uuid.uuid4())
    t0 = time.perf_counter()

    if req.lat is None or req.lon is None:
        glat, glon = _geocode_location(req.location)
        if glat is not None and glon is not None:
            req.lat = glat
            req.lon = glon

    rows = []
    diagnostics = {
        "engine": "serpapi_google_maps",
        "url_query": f"{req.query} in {req.location}",
        "lat": req.lat,
        "lon": req.lon,
        "pages_fetched": 0,
        "raw_total": 0,
        "after_dedupe": 0,
        "after_quality_filter": 0,
        "after_max_results": 0,
        "details_fetched": 0,
        "serpapi_status": None,
        "errors": [],
    }

    raw_results, page_diag = _serpapi_search(
        req.query, req.location, req.lat, req.lon, req.zoom, max_pages=3
    )
    diagnostics["pages_fetched"] = page_diag["pages_fetched"]
    diagnostics["raw_total"] = page_diag["raw_total"]
    diagnostics["serpapi_status"] = page_diag["serpapi_status"]
    diagnostics["errors"].extend(page_diag["errors"])

    rows = [_normalize_row(item) for item in raw_results]

    if req.fetch_details:
        detail_count = 0
        max_fetch = req.max_detail_fetches if req.max_detail_fetches > 0 else len(rows)
        for r in rows:
            if detail_count >= max_fetch:
                break
            if r.get("phone") and r.get("website"):
                continue
            pid = r.get("place_id")
            if not pid:
                continue
            details = _serpapi_place_details(pid)
            if not details:
                continue
            detail_count += 1
            if not r["phone"] and details.get("phone"):
                r["phone"] = _clean(details["phone"])
            if not r["website"] and details.get("website") and not _is_junk_url(details.get("website", "")):
                r["website"] = _clean(details["website"])
            if not r["address"] and details.get("address"):
                r["address"] = _clean(details["address"])
            if not r["rating"] and details.get("rating"):
                r["rating"] = str(details["rating"])
            if not r["reviews"] and details.get("reviews"):
                r["reviews"] = str(details["reviews"])
            if not r["category"] and details.get("type"):
                r["category"] = _clean(details["type"])
        diagnostics["details_fetched"] = detail_count

    rows = _strip_junk_urls(rows)
    rows = _dedupe(rows)
    diagnostics["after_dedupe"] = len(rows)

    rows.sort(key=lambda r: _quality_score(r), reverse=True)
    if req.max_results and req.max_results > 0:
        rows = rows[:req.max_results]
    diagnostics["after_max_results"] = len(rows)

    elapsed = time.perf_counter() - t0

    payload = {
        "request_id": request_id,
        "total_results": len(rows),
        "with_rating": sum(1 for r in rows if r["rating"]),
        "with_phone": sum(1 for r in rows if r["phone"]),
        "with_website": sum(1 for r in rows if r["website"]),
        "with_address": sum(1 for r in rows if r["address"]),
        "elapsed_seconds": round(elapsed, 2),
        "diagnostics": diagnostics,
        "results": rows,
    }

    out_file = OUTPUT_DIR / f"{request_id}.json"
    with open(out_file, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    return payload


@app.get("/download/{request_id}")
async def download_csv(request_id: str):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", request_id)
    json_path = OUTPUT_DIR / f"{safe_id}.json"
    if not json_path.exists():
        raise HTTPException(status_code=404, detail="request not found")

    with open(json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    rows = data.get("results", [])
    fieldnames = [
        "name", "category", "rating", "reviews", "phone",
        "website", "address", "lat", "lon", "place_id",
    ]

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    buffer.seek(0)

    filename = f"places_{safe_id}.csv"
    return StreamingResponse(
        iter([buffer.getvalue().encode("utf-8")]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/health")
async def health():
    return {
        "status": "ok" if SERPAPI_API_KEY else "no_api_key",
        "version": "1.0.0",
        "serpapi_key_present": bool(SERPAPI_API_KEY),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
