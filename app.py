import os
from datetime import datetime, timezone

import requests
import re
from urllib.parse import urljoin, urlparse
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=None)

STAKE_BASE_URL = os.getenv("STAKE_BASE_URL", "https://odds-data.stake.com").rstrip("/")
STAKE_API_KEY = os.getenv("STAKE_API_KEY", "").strip()
STAKE_API_KEY_HEADER = os.getenv("STAKE_API_KEY_HEADER", "apiKey").strip()
STAKE_SPORT = os.getenv("STAKE_SPORT", "football").strip()
STAKE_FIXTURE_LIMIT = int(os.getenv("STAKE_FIXTURE_LIMIT", "20"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))


def stake_headers():
    headers = {"Accept": "application/json"}
    if STAKE_API_KEY:
        headers[STAKE_API_KEY_HEADER] = STAKE_API_KEY
    return headers


def stake_get(path):
    r = requests.get(
        f"{STAKE_BASE_URL}{path}",
        headers=stake_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def extract_fixture_list(payload):
    if isinstance(payload, dict):
        for key in ("fixture", "fixtures", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    if isinstance(payload, list):
        return payload
    return []


def top_fixture(payload):
    if isinstance(payload, dict):
        f = payload.get("fixture")
        if isinstance(f, dict):
            return f
        return payload
    return {}


def find_market_nodes(obj, path="$", found=None, limit=2000):
    """
    Recursively scan the entire fixture response.

    A market node is any dict containing an `outcomes` list with at least
    two usable outcome records. This avoids depending on Stake's exact
    nesting level (groups, event groups, market groups, etc.).
    """
    if found is None:
        found = []

    if len(found) >= limit:
        return found

    if isinstance(obj, dict):
        outcomes = obj.get("outcomes")

        if isinstance(outcomes, list):
            usable = []
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue

                selection = str(outcome.get("name", "")).strip()

                try:
                    odds = float(outcome.get("odds"))
                except (TypeError, ValueError):
                    continue

                if selection and odds > 1:
                    usable.append({
                        "selection": selection,
                        "odds": odds,
                    })

            if len(usable) in (2, 3):
                found.append({
                    "path": path,
                    "node": obj,
                    "outcomes": usable,
                })

                if len(found) >= limit:
                    return found

        for key, value in obj.items():
            find_market_nodes(value, f"{path}.{key}", found, limit)

    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            find_market_nodes(value, f"{path}[{i}]", found, limit)

    return found


def parse_fixture_recursive(payload):
    fixture = top_fixture(payload)

    event_id = str(
        fixture.get("id")
        or fixture.get("extId")
        or fixture.get("slug")
        or ""
    )
    event_name = str(fixture.get("name", ""))
    event_slug = str(fixture.get("slug", ""))
    start_time = fixture.get("startTime") or fixture.get("date")
    tournament = fixture.get("tournament")
    category = fixture.get("category")
    sport = fixture.get("sport") or STAKE_SPORT

    market_nodes = find_market_nodes(payload)
    parsed = []
    seen = set()

    for item in market_nodes:
        node = item["node"]
        outcomes = item["outcomes"]

        market_name = str(
            node.get("name")
            or node.get("marketName")
            or node.get("label")
            or node.get("title")
            or "market"
        ).strip()

        # Deduplicate repeated appearances of the same market node.
        sig = (
            market_name.lower(),
            tuple((o["selection"].lower(), o["odds"]) for o in outcomes),
        )
        if sig in seen:
            continue
        seen.add(sig)

        parsed.append({
            "bookmaker": "Stake",
            "eventId": event_id,
            "event": event_name,
            "slug": event_slug,
            "startTime": start_time,
            "sport": sport,
            "tournament": tournament,
            "category": category,
            "market": market_name,
            "jsonPath": item["path"],
            "outcomes": outcomes,
        })

    return parsed


def fetch_stake_markets():
    if not STAKE_API_KEY:
        return {
            "ok": False,
            "fixtures": [],
            "markets": [],
            "errors": ["STAKE_API_KEY is not configured"],
        }

    errors = []
    fixtures_meta = []
    markets = []

    try:
        fixture_list_payload = stake_get(f"/sport/{STAKE_SPORT}/fixture")
        fixtures = extract_fixture_list(fixture_list_payload)
    except Exception as exc:
        return {
            "ok": False,
            "fixtures": [],
            "markets": [],
            "errors": [f"fixture-list: {type(exc).__name__}: {exc}"],
        }

    for fixture in fixtures[:STAKE_FIXTURE_LIMIT]:
        if not isinstance(fixture, dict):
            continue

        slug = str(fixture.get("slug") or fixture.get("id") or "").strip()
        if not slug:
            continue

        fixtures_meta.append({
            "slug": slug,
            "id": fixture.get("id"),
            "name": fixture.get("name"),
            "startTime": fixture.get("startTime") or fixture.get("date"),
            "tournament": fixture.get("tournament"),
            "category": fixture.get("category"),
        })

        try:
            raw = stake_get(f"/fixtures/{slug}")
            markets.extend(parse_fixture_recursive(raw))
        except Exception as exc:
            errors.append(f"{slug}: {type(exc).__name__}: {exc}")

    return {
        "ok": len(errors) == 0,
        "fixtures": fixtures_meta,
        "markets": markets,
        "errors": errors,
    }


@app.get("/")
def home():
    root = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(root, "index.html")


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "stakeConfigured": bool(STAKE_API_KEY),
        "time": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/stake-debug")
def stake_debug():
    data = fetch_stake_markets()

    return jsonify({
        "ok": data["ok"],
        "configured": bool(STAKE_API_KEY),
        "baseUrl": STAKE_BASE_URL,
        "sport": STAKE_SPORT,
        "fixtureLimit": STAKE_FIXTURE_LIMIT,
        "fixturesLoaded": len(data["fixtures"]),
        "marketsLoaded": len(data["markets"]),
        "quotesLoaded": sum(len(m["outcomes"]) for m in data["markets"]),
        "sampleFixtures": data["fixtures"][:5],
        "sampleMarkets": data["markets"][:10],
        "errors": data["errors"][:20],
    })


@app.get("/api/stake-markets")
def stake_markets():
    data = fetch_stake_markets()

    return jsonify({
        "ok": data["ok"],
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "fixturesLoaded": len(data["fixtures"]),
        "marketsLoaded": len(data["markets"]),
        "quotesLoaded": sum(len(m["outcomes"]) for m in data["markets"]),
        "markets": data["markets"],
        "errors": data["errors"],
    })


@app.get("/api/opportunities")
def opportunities():
    bankroll = float(request.args.get("bankroll", os.getenv("BANKROLL", "1000")))
    minimum = float(request.args.get(
        "min_arb_percent",
        os.getenv("MIN_ARB_PERCENT", "0.5")
    ))

    stake_data = fetch_stake_markets()

    # Stake-only stage: we expose the loaded live quote count.
    # Arbitrage needs a second bookmaker feed.
    return jsonify({
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "bankroll": bankroll,
        "minArbPercent": minimum,
        "stakeMarketsLoaded": len(stake_data["markets"]),
        "stakeQuotesLoaded": sum(len(m["outcomes"]) for m in stake_data["markets"]),
        "opportunities": [],
        "errors": stake_data["errors"],
        "note": "Stake live markets loaded recursively. Add SpinAndBet markets to calculate cross-book arbitrage.",
    })


def spinandbet_public_discover(target_url):
    """
    Public-page discovery only.
    Fetches a SpinAndBet page without login/session cookies and inspects the
    returned HTML for script URLs and API-like endpoint strings.
    It does not bypass CAPTCHA, authentication, or anti-bot controls.
    """
    if not target_url:
        return {"ok": False, "error": "No URL supplied"}

    parsed = urlparse(target_url)
    if parsed.scheme not in ("http", "https"):
        return {"ok": False, "error": "URL must start with http:// or https://"}

    if not parsed.hostname or not parsed.hostname.endswith("spinandbet.net"):
        return {"ok": False, "error": "Only spinandbet.net URLs are allowed"}

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "User-Agent": "Mozilla/5.0",
    }

    try:
        r = requests.get(target_url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except Exception as exc:
        return {
            "ok": False,
            "errorType": type(exc).__name__,
            "error": str(exc),
        }

    text = r.text or ""

    script_srcs = []
    for m in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', text, flags=re.I):
        script_srcs.append(urljoin(r.url, m.group(1)))

    # Collect URL-like strings that may identify public data endpoints.
    url_candidates = set()
    patterns = [
        r'https?://[^"\'<>\s]+',
        r'/(?:api|sports|sport|event|events|fixture|fixtures|market|markets|odds|prematch|pre-match)[^"\'<>\s]*',
    ]

    for pattern in patterns:
        for m in re.finditer(pattern, text, flags=re.I):
            candidate = m.group(0).rstrip(");,")
            if len(candidate) > 4:
                url_candidates.add(urljoin(r.url, candidate))

    # Keep likely sports-data candidates first.
    keywords = ("api", "odds", "event", "fixture", "market", "prematch", "pre-match", "sports")
    ranked = sorted(
        url_candidates,
        key=lambda u: (
            -sum(1 for k in keywords if k in u.lower()),
            len(u),
        )
    )

    # Extract obvious numeric event id from the supplied URL.
    event_ids = re.findall(r'/(\d{6,})(?:/|$|\?)', parsed.path)

    return {
        "ok": r.ok,
        "httpStatus": r.status_code,
        "finalUrl": r.url,
        "contentType": r.headers.get("content-type"),
        "htmlLength": len(text),
        "eventIdsFromUrl": event_ids[:10],
        "scriptCount": len(script_srcs),
        "scripts": script_srcs[:50],
        "candidateCount": len(ranked),
        "candidates": ranked[:100],
        "pagePreview": re.sub(r'\s+', ' ', text[:1200]),
        "note": "Public-page inspection only; no login cookies or access-control bypass used.",
    }


@app.get("/api/spinandbet-discover")
def spinandbet_discover():
    target_url = request.args.get(
        "url",
        os.getenv(
            "SPINANDBET_SEED_URL",
            "https://m.spinandbet.net/en/sports/pre-match/event-view/Soccer/England/538/premier-league/30086420/arsenal-coventry-city?s=72"
        )
    ).strip()

    return jsonify(spinandbet_public_discover(target_url))
