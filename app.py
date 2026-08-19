import os
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=None)

STAKE_BASE_URL = os.getenv("STAKE_BASE_URL", "https://odds-data.stake.com").rstrip("/")
STAKE_API_KEY = os.getenv("STAKE_API_KEY", "").strip()
STAKE_API_KEY_HEADER = os.getenv("STAKE_API_KEY_HEADER", "apiKey").strip()
STAKE_SPORTS = [x.strip() for x in os.getenv("STAKE_SPORTS", "football").split(",") if x.strip()]
STAKE_FIXTURE_LIMIT = int(os.getenv("STAKE_FIXTURE_LIMIT", "20"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))


def stake_headers():
    headers = {"Accept": "application/json"}
    if STAKE_API_KEY:
        headers[STAKE_API_KEY_HEADER] = STAKE_API_KEY
    return headers


def stake_get(path):
    url = f"{STAKE_BASE_URL}{path}"
    r = requests.get(url, headers=stake_headers(), timeout=REQUEST_TIMEOUT)
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
        fixture = payload.get("fixture", payload)
        if isinstance(fixture, dict):
            return fixture
    return {}


def parse_markets(fixture_payload):
    fixture = normalize_fixture(fixture_payload)
    if not fixture:
        return []

    fixture_id = str(
        fixture.get("id")
        or fixture.get("extId")
        or fixture.get("slug")
        or ""
    )
    event_name = str(fixture.get("name", ""))
    league = str(
        fixture.get("tournament")
        or fixture.get("league")
        or fixture.get("category")
        or ""
    )
    sport = str(fixture.get("sport", "football"))
    start_time = fixture.get("startTime") or fixture.get("date")

    markets_out = []

    for group in fixture.get("groups", []) or []:
        if not isinstance(group, dict):
            continue

        group_name = str(group.get("name", ""))

        for market in group.get("markets", []) or []:
            if not isinstance(market, dict):
                continue

            status = str(market.get("status", "active")).lower()
            if status not in ("active", ""):
                continue

            market_name = str(market.get("name") or group_name or "market")
            outcomes = []

            for outcome in market.get("outcomes", []) or []:
                if not isinstance(outcome, dict):
                    continue

                if outcome.get("active") is False:
                    continue

                name = str(outcome.get("name", "")).strip()

                try:
                    odds = float(outcome.get("odds"))
                except (TypeError, ValueError):
                    continue

                if not name or odds <= 1:
                    continue

                outcomes.append({
                    "name": name,
                    "odds": odds,
                })

            # Keep common head-to-head markets:
            # 2 outcomes = moneyline/two-way
            # 3 outcomes = football 1X2/home-draw-away
            if len(outcomes) not in (2, 3):
                continue

            markets_out.append({
                "bookmaker": "Stake",
                "event_id": fixture_id,
                "event": event_name,
                "sport": sport,
                "league": league,
                "startTime": start_time,
                "group": group_name,
                "market": market_name,
                "outcomes": outcomes,
            })

    return markets_out


def fetch_fixture_slugs():
    fixture_slugs = []
    fixtures_meta = []

    for sport_slug in STAKE_SPORTS:
        payload = stake_get(f"/sport/{sport_slug}/fixture")
        fixtures = extract_fixture_list(payload)

        for fixture in fixtures:
            if not isinstance(fixture, dict):
                continue

            slug = str(fixture.get("slug") or fixture.get("id") or "").strip()
            if not slug:
                continue

            fixture_slugs.append(slug)
            fixtures_meta.append({
                "slug": slug,
                "id": fixture.get("id"),
                "name": fixture.get("name"),
                "startTime": fixture.get("startTime") or fixture.get("date"),
                "tournament": fixture.get("tournament"),
                "category": fixture.get("category"),
            })

            if len(fixture_slugs) >= STAKE_FIXTURE_LIMIT:
                return fixture_slugs, fixtures_meta

    return fixture_slugs, fixtures_meta


def fetch_stake_markets():
    if not STAKE_API_KEY:
        return {
            "configured": False,
            "fixtures": [],
            "markets": [],
            "errors": ["STAKE_API_KEY is not configured"],
        }

    errors = []

    try:
        fixture_slugs, fixtures_meta = fetch_fixture_slugs()
    except Exception as exc:
        return {
            "configured": True,
            "fixtures": [],
            "markets": [],
            "errors": [f"fixture-list: {type(exc).__name__}: {exc}"],
        }

    all_markets = []

    for slug in fixture_slugs:
        try:
            payload = stake_get(f"/fixtures/{slug}")
            all_markets.extend(parse_markets(payload))
        except Exception as exc:
            errors.append(f"{slug}: {type(exc).__name__}: {exc}")

    return {
        "configured": True,
        "fixtures": fixtures_meta,
        "markets": all_markets,
        "errors": errors,
    }


def market_signature(market):
    names = tuple(sorted(str(x["name"]).strip().lower() for x in market["outcomes"]))
    return (
        str(market["event"]).strip().lower(),
        str(market["market"]).strip().lower(),
        names,
    )


def find_cross_book_arbs(markets, bankroll, min_arb_percent):
    """
    General 2-way/3-way arbitrage engine.

    Requires equivalent markets from at least two bookmakers.
    At the moment only Stake is loaded, so this normally returns [] until
    SpinAndBet or another second-book feed is added.
    """
    groups = {}

    for m in markets:
        key = market_signature(m)
        groups.setdefault(key, []).append(m)

    results = []

    for key, group in groups.items():
        bookmakers = {m["bookmaker"] for m in group}
        if len(bookmakers) < 2:
            continue

        outcome_names = [x["name"] for x in group[0]["outcomes"]]
        best = {}

        for name in outcome_names:
            candidates = []
            for m in group:
                for o in m["outcomes"]:
                    if str(o["name"]).strip().lower() == str(name).strip().lower():
                        candidates.append({
                            "bookmaker": m["bookmaker"],
                            "name": name,
                            "odds": float(o["odds"]),
                        })

            if not candidates:
                break

            best[name] = max(candidates, key=lambda x: x["odds"])

        if len(best) != len(outcome_names):
            continue

        arb_sum = sum(1 / x["odds"] for x in best.values())

        if arb_sum >= 1:
            continue

        arb_percent = (1 - arb_sum) * 100
        if arb_percent < min_arb_percent:
            continue

        legs = []
        guaranteed_return = bankroll / arb_sum

        for outcome_name, quote in best.items():
            stake = guaranteed_return / quote["odds"]
            legs.append({
                "selection": outcome_name,
                "bookmaker": quote["bookmaker"],
                "odds": quote["odds"],
                "stake": round(stake, 2),
            })

        results.append({
            "event": group[0]["event"],
            "market": group[0]["market"],
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
        "ok": data["configured"] and not data["errors"],
        "configured": data["configured"],
        "baseUrl": STAKE_BASE_URL,
        "sports": STAKE_SPORTS,
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
        "ok": data["configured"] and not data["errors"],
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "fixturesLoaded": len(data["fixtures"]),
        "marketsLoaded": len(data["markets"]),
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

    data = fetch_stake_markets()
    results = find_cross_book_arbs(data["markets"], bankroll, minimum)

    return jsonify({
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "stakeMarketsLoaded": len(data["markets"]),
        "opportunities": results,
        "errors": data["errors"],
        "note": "Stake is live. Arbitrage requires matching odds from a second bookmaker such as SpinAndBet.",
    })
