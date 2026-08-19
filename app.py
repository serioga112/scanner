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
    print(f"STAKE GET {url}", flush=True)
    r = requests.get(url, headers=stake_headers(), timeout=REQUEST_TIMEOUT)
    print(f"STAKE STATUS {r.status_code} {path}", flush=True)
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


def parse_two_way_markets(fixture_payload):
    fixture = fixture_payload.get("fixture", fixture_payload) if isinstance(fixture_payload, dict) else {}
    if not isinstance(fixture, dict):
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
    sport = str(fixture.get("sport", ""))

    quotes = []
    groups = fixture.get("groups", []) or []

    for group in groups:
        markets = group.get("markets", []) if isinstance(group, dict) else []
        for market in markets or []:
            market_name = str(
                market.get("name")
                or (group.get("name") if isinstance(group, dict) else "")
                or "market"
            )

            outcomes = market.get("outcomes", []) or []
            parsed = []

            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                name = str(outcome.get("name", "")).strip()
                raw_odds = outcome.get("odds")

                try:
                    odds = float(raw_odds)
                except (TypeError, ValueError):
                    continue

                if name and odds > 1:
                    parsed.append((name, odds))

            if len(parsed) == 2:
                for name, odds in parsed:
                    quotes.append({
                        "bookmaker": "Stake",
                        "event_id": fixture_id,
                        "event": event_name,
                        "sport": sport,
                        "league": league,
                        "market": market_name,
                        "selection": name,
                        "odds": odds,
                    })

    return quotes


def fetch_stake_debug():
    result = {
        "configured": bool(STAKE_API_KEY),
        "baseUrl": STAKE_BASE_URL,
        "sports": STAKE_SPORTS,
        "fixtureLimit": STAKE_FIXTURE_LIMIT,
        "fixturesLoaded": 0,
        "quotesLoaded": 0,
        "sampleFixtures": [],
        "sampleQuotes": [],
        "errors": [],
    }

    if not STAKE_API_KEY:
        result["errors"].append("STAKE_API_KEY is not configured")
        return result

    fixture_slugs = []

    for sport_slug in STAKE_SPORTS:
        try:
            payload = stake_get(f"/sport/{sport_slug}/fixture")
            fixtures = extract_fixture_list(payload)

            for fixture in fixtures:
                if not isinstance(fixture, dict):
                    continue

                slug = str(
                    fixture.get("slug")
                    or fixture.get("id")
                    or ""
                ).strip()

                if not slug:
                    continue

                fixture_slugs.append(slug)

                if len(result["sampleFixtures"]) < 5:
                    result["sampleFixtures"].append({
                        "slug": slug,
                        "name": fixture.get("name"),
                        "id": fixture.get("id"),
                    })

                if len(fixture_slugs) >= STAKE_FIXTURE_LIMIT:
                    break

        except Exception as exc:
            result["errors"].append(f"{sport_slug}: {type(exc).__name__}: {exc}")

        if len(fixture_slugs) >= STAKE_FIXTURE_LIMIT:
            break

    result["fixturesLoaded"] = len(fixture_slugs)

    all_quotes = []

    for slug in fixture_slugs:
        try:
            payload = stake_get(f"/fixtures/{slug}")
            quotes = parse_two_way_markets(payload)
            all_quotes.extend(quotes)
        except Exception as exc:
            result["errors"].append(f"{slug}: {type(exc).__name__}: {exc}")

    result["quotesLoaded"] = len(all_quotes)
    result["sampleQuotes"] = all_quotes[:10]

    return result


def find_arbs(quotes, bankroll, min_arb_percent):
    groups = {}

    for q in quotes:
        try:
            odds = float(q["odds"])
        except (KeyError, TypeError, ValueError):
            continue

        if odds <= 1:
            continue

        key = f'{str(q["event_id"]).strip().lower()}|{str(q["market"]).strip().lower()}'
        groups.setdefault(key, []).append(q)

    results = []

    for key, group in groups.items():
        best = {}

        for q in group:
            selection_key = str(q["selection"]).strip().lower()
            if selection_key not in best or float(q["odds"]) > float(best[selection_key]["odds"]):
                best[selection_key] = q

        if len(best) != 2:
            continue

        a, b = list(best.values())
        odds_a = float(a["odds"])
        odds_b = float(b["odds"])

        arb_sum = (1 / odds_a) + (1 / odds_b)

        if arb_sum >= 1:
            continue

        arb_percent = (1 - arb_sum) * 100

        if arb_percent < min_arb_percent:
            continue

        stake_a = bankroll * (1 / odds_a) / arb_sum
        stake_b = bankroll * (1 / odds_b) / arb_sum
        guaranteed_return = stake_a * odds_a

        results.append({
            "id": key,
            "event": a.get("event") or b.get("event") or "",
            "sport": a.get("sport") or b.get("sport") or "",
            "league": a.get("league") or b.get("league") or "",
            "market": a.get("market", ""),
            "selectionA": a.get("selection", ""),
            "bookmakerA": a.get("bookmaker", ""),
            "oddsA": odds_a,
            "selectionB": b.get("selection", ""),
            "bookmakerB": b.get("bookmaker", ""),
            "oddsB": odds_b,
            "arbPercent": round(arb_percent, 4),
            "stakeA": round(stake_a, 2),
            "stakeB": round(stake_b, 2),
            "guaranteedReturn": round(guaranteed_return, 2),
            "profit": round(guaranteed_return - bankroll, 2),
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
    data = fetch_stake_debug()
    data["ok"] = data["configured"] and not data["errors"]
    return jsonify(data)


@app.get("/api/opportunities")
def opportunities():
    bankroll = float(request.args.get("bankroll", os.getenv("BANKROLL", "1000")))
    minimum = float(request.args.get(
        "min_arb_percent",
        os.getenv("MIN_ARB_PERCENT", "0.5")
    ))

    use_demo = os.getenv("USE_DEMO_DATA", "true").lower() in ("1", "true", "yes")

    if use_demo:
        return jsonify({
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "quoteCount": 0,
            "opportunities": [],
            "mode": "demo-disabled-for-debug",
        })

    debug = fetch_stake_debug()
    quotes = debug.get("sampleQuotes", [])

    return jsonify({
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "quoteCount": debug.get("quotesLoaded", 0),
        "opportunities": find_arbs(quotes, bankroll, minimum),
        "stakeDebug": {
            "fixturesLoaded": debug.get("fixturesLoaded", 0),
            "quotesLoaded": debug.get("quotesLoaded", 0),
            "errors": debug.get("errors", []),
        }
    })
