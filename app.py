import os
from datetime import datetime, timezone

import requests
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


def fixture_meta(fixture):
    return {
        "id": fixture.get("id"),
        "slug": fixture.get("slug"),
        "name": fixture.get("name"),
        "date": fixture.get("date"),
        "status": fixture.get("status"),
        "tournamentId": fixture.get("tournamentId"),
    }


def parse_fixture_markets(payload):
    """
    Stake's fixture response exposes markets directly under fixture["markets"].
    Each market contains an outcomes array with decimal odds.
    """
    if not isinstance(payload, dict):
        return []

    fixture = payload.get("fixture", payload)
    if not isinstance(fixture, dict):
        return []

    event_id = str(fixture.get("id") or fixture.get("slug") or "")
    event_name = str(fixture.get("name", ""))
    event_slug = str(fixture.get("slug", ""))
    start_time = fixture.get("date") or fixture.get("startTime")
    tournament_id = fixture.get("tournamentId")

    parsed_markets = []

    for market in fixture.get("markets", []) or []:
        if not isinstance(market, dict):
            continue

        if market.get("active") is False:
            continue

        market_name = str(market.get("name", "")).strip()
        outcomes = []

        for outcome in market.get("outcomes", []) or []:
            if not isinstance(outcome, dict):
                continue

            if outcome.get("active") is False:
                continue

            selection = str(outcome.get("name", "")).strip()
            try:
                odds = float(outcome.get("odds"))
            except (TypeError, ValueError):
                continue

            if not selection or odds <= 1:
                continue

            outcomes.append({
                "selection": selection,
                "odds": odds,
            })

        # Keep standard H2H markets (2-way and 3-way).
        if len(outcomes) not in (2, 3):
            continue

        parsed_markets.append({
            "bookmaker": "Stake",
            "eventId": event_id,
            "event": event_name,
            "slug": event_slug,
            "startTime": start_time,
            "tournamentId": tournament_id,
            "market": market_name,
            "outcomes": outcomes,
        })

    return parsed_markets


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

        fixtures_meta.append(fixture_meta(fixture))

        try:
            raw = stake_get(f"/fixtures/{slug}")
            markets.extend(parse_fixture_markets(raw))
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


@app.get("/api/stake-markets")
def stake_markets():
    data = fetch_stake_markets()

    return jsonify({
        "ok": data["ok"],
        "fixturesLoaded": len(data["fixtures"]),
        "marketsLoaded": len(data["markets"]),
        "quotesLoaded": sum(len(m["outcomes"]) for m in data["markets"]),
        "markets": data["markets"],
        "errors": data["errors"],
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


@app.get("/api/opportunities")
def opportunities():
    # Stake-only mode: return the market count for now.
    # Arbitrage is only computed after a second bookmaker feed is connected.
    data = fetch_stake_markets()

    return jsonify({
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "stakeMarketsLoaded": len(data["markets"]),
        "stakeQuotesLoaded": sum(len(m["outcomes"]) for m in data["markets"]),
        "opportunities": [],
        "errors": data["errors"],
        "note": "Stake live odds loaded. Connect SpinAndBet to calculate cross-book arbitrage.",
    })
