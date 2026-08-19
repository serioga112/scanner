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
    r = requests.get(
        f"{STAKE_BASE_URL}{path}",
        headers=stake_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


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
    league = str(fixture.get("tournament", ""))
    sport = str(fixture.get("sport", ""))

    quotes = []

    # Common market structure: groups -> markets -> outcomes
    groups = fixture.get("groups", []) or []

    for group in groups:
        for market in group.get("markets", []) or []:
            market_name = str(market.get("name") or group.get("name") or "market")
            outcomes = market.get("outcomes", []) or []

            parsed = []
            for outcome in outcomes:
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


def fetch_stake_quotes():
    if not STAKE_API_KEY:
        return []

    quotes = []
    fixture_slugs = []

    for sport_slug in STAKE_SPORTS:
        payload = stake_get(f"/sport/{sport_slug}/fixture")

        if isinstance(payload, dict):
            fixtures = payload.get("fixture", [])
        else:
            fixtures = []

        for fixture in fixtures or []:
            slug = str(fixture.get("slug", "")).strip()
            if slug:
                fixture_slugs.append(slug)

            if len(fixture_slugs) >= STAKE_FIXTURE_LIMIT:
                break

        if len(fixture_slugs) >= STAKE_FIXTURE_LIMIT:
            break

    for slug in fixture_slugs:
        try:
            payload = stake_get(f"/fixtures/{slug}")
            quotes.extend(parse_two_way_markets(payload))
        except requests.RequestException:
            continue

    return quotes


def demo_quotes():
    return [
        {
            "bookmaker": "SpinAndBet",
            "event_id": "demo-1",
            "event": "Team A vs Team B",
            "sport": "football",
            "league": "Demo League",
            "market": "moneyline",
            "selection": "Team A",
            "odds": 2.35,
        },
        {
            "bookmaker": "SpinAndBet",
            "event_id": "demo-1",
            "event": "Team A vs Team B",
            "sport": "football",
            "league": "Demo League",
            "market": "moneyline",
            "selection": "Team B",
            "odds": 1.82,
        },
        {
            "bookmaker": "Stake",
            "event_id": "demo-1",
            "event": "Team A vs Team B",
            "sport": "football",
            "league": "Demo League",
            "market": "moneyline",
            "selection": "Team A",
            "odds": 1.91,
        },
        {
            "bookmaker": "Stake",
            "event_id": "demo-1",
            "event": "Team A vs Team B",
            "sport": "football",
            "league": "Demo League",
            "market": "moneyline",
            "selection": "Team B",
            "odds": 2.05,
        },
    ]


def get_all_quotes():
    use_demo = os.getenv("USE_DEMO_DATA", "true").lower() in ("1", "true", "yes")
    if use_demo:
        return demo_quotes()

    # For now we only pull Stake live.
    # SpinAndBet can be added later through a permitted feed or manual input.
    return fetch_stake_quotes()


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


@app.get("/api/stake-status")
def stake_status():
    if not STAKE_API_KEY:
        return jsonify({
            "ok": False,
            "error": "STAKE_API_KEY is not configured",
        }), 400

    try:
        sports = stake_get("/sports")
        return jsonify({
            "ok": True,
            "sportsCount": len(sports) if isinstance(sports, list) else None,
            "sample": sports[:5] if isinstance(sports, list) else sports,
        })
    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 502


@app.get("/api/opportunities")
def opportunities():
    bankroll = float(request.args.get("bankroll", os.getenv("BANKROLL", "1000")))
    minimum = float(request.args.get(
        "min_arb_percent",
        os.getenv("MIN_ARB_PERCENT", "0.5")
    ))

    try:
        quotes = get_all_quotes()
        results = find_arbs(quotes, bankroll, minimum)

        return jsonify({
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "quoteCount": len(quotes),
            "opportunities": results,
        })

    except Exception as exc:
        return jsonify({
            "error": str(exc),
            "opportunities": [],
        }), 502
