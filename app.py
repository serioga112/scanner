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


def normalize_fixture(payload):
    if isinstance(payload, dict):
        f = payload.get("fixture", payload)
        if isinstance(f, dict):
            return f
    return {}


def parse_fixture_groups(payload):
    fixture = normalize_fixture(payload)
    if not fixture:
        return []

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

    parsed = []

    groups = fixture.get("groups", []) or []

    for group in groups:
        if not isinstance(group, dict):
            continue

        group_name = str(group.get("name", "")).strip()

        for market in group.get("markets", []) or []:
            if not isinstance(market, dict):
                continue

            market_name = str(market.get("name") or group_name or "market").strip()
            outcomes = []

            for outcome in market.get("outcomes", []) or []:
                if not isinstance(outcome, dict):
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

            # Keep practical H2H markets only:
            # 2-way = winner / draw-no-bet style
            # 3-way = football 1X2
            if len(outcomes) not in (2, 3):
                continue

            parsed.append({
                "bookmaker": "Stake",
                "eventId": event_id,
                "event": event_name,
                "slug": event_slug,
                "startTime": start_time,
                "sport": sport,
                "tournament": tournament,
                "category": category,
                "group": group_name,
                "market": market_name,
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
            markets.extend(parse_fixture_groups(raw))
        except Exception as exc:
            errors.append(f"{slug}: {type(exc).__name__}: {exc}")

    return {
        "ok": len(errors) == 0,
        "fixtures": fixtures_meta,
        "markets": markets,
        "errors": errors,
    }


def canonical_market_key(m):
    selections = tuple(sorted(
        str(o["selection"]).strip().lower()
        for o in m["outcomes"]
    ))
    return (
        str(m["event"]).strip().lower(),
        str(m["market"]).strip().lower(),
        selections,
    )


def find_cross_book_arbs(markets, bankroll, min_arb_percent):
    groups = {}

    for m in markets:
        groups.setdefault(canonical_market_key(m), []).append(m)

    results = []

    for _, group in groups.items():
        bookmakers = {m["bookmaker"] for m in group}
        if len(bookmakers) < 2:
            continue

        reference = group[0]
        outcome_names = [o["selection"] for o in reference["outcomes"]]

        best = {}

        for name in outcome_names:
            candidates = []
            target = str(name).strip().lower()

            for m in group:
                for o in m["outcomes"]:
                    if str(o["selection"]).strip().lower() == target:
                        candidates.append({
                            "bookmaker": m["bookmaker"],
                            "selection": name,
                            "odds": float(o["odds"]),
                        })

            if not candidates:
                best = {}
                break

            best[name] = max(candidates, key=lambda x: x["odds"])

        if len(best) != len(outcome_names):
            continue

        arb_sum = sum(1 / leg["odds"] for leg in best.values())

        if arb_sum >= 1:
            continue

        arb_percent = (1 - arb_sum) * 100

        if arb_percent < min_arb_percent:
            continue

        guaranteed_return = bankroll / arb_sum
        legs = []

        for selection, leg in best.items():
            stake = guaranteed_return / leg["odds"]
            legs.append({
                "selection": selection,
                "bookmaker": leg["bookmaker"],
                "odds": leg["odds"],
                "stake": round(stake, 2),
            })

        results.append({
            "event": reference["event"],
            "market": reference["market"],
            "arbPercent": round(arb_percent, 4),
            "guaranteedReturn": round(guaranteed_return, 2),
            "profit": round(guaranteed_return - bankroll, 2),
            "legs": legs,
        })

    return sorted(results, key=lambda x: x["arbPercent"], reverse=True)


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

    # Right now only Stake is live. Once SpinAndBet markets are added
    # to this list, cross-book arbitrage starts working automatically.
    all_markets = list(stake_data["markets"])

    return jsonify({
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "stakeMarketsLoaded": len(stake_data["markets"]),
        "stakeQuotesLoaded": sum(len(m["outcomes"]) for m in stake_data["markets"]),
        "opportunities": find_cross_book_arbs(all_markets, bankroll, minimum),
        "errors": stake_data["errors"],
        "note": "Stake live markets are loaded. Add SpinAndBet markets to enable cross-book arbitrage.",
    })
