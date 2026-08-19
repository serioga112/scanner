import os
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=None)

REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))


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


def fetch_generic_json_feed(bookmaker, url_env, header_env, value_env):
    url = os.getenv(url_env, "").strip()
    if not url:
        return []

    headers = {"Accept": "application/json"}
    header_name = os.getenv(header_env, "").strip()
    header_value = os.getenv(value_env, "").strip()

    if header_name and header_value:
        headers[header_name] = header_value

    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, dict):
        rows = payload.get("odds", payload.get("data", payload.get("results", [])))
    else:
        rows = payload

    quotes = []

    for item in rows or []:
        try:
            odds = float(item["odds"])
            if odds <= 1:
                continue

            quotes.append({
                "bookmaker": bookmaker,
                "event_id": str(item["event_id"]),
                "event": str(item.get("event", "")),
                "sport": str(item.get("sport", "")),
                "league": str(item.get("league", "")),
                "market": str(item["market"]),
                "selection": str(item["selection"]),
                "odds": odds,
            })
        except (KeyError, TypeError, ValueError):
            continue

    return quotes


def get_all_quotes():
    use_demo = os.getenv("USE_DEMO_DATA", "true").lower() in ("1", "true", "yes")
    if use_demo:
        return demo_quotes()

    quotes = []

    quotes += fetch_generic_json_feed(
        "Stake",
        "STAKE_FEED_URL",
        "STAKE_AUTH_HEADER",
        "STAKE_AUTH_VALUE",
    )

    quotes += fetch_generic_json_feed(
        "SpinAndBet",
        "SPINANDBET_FEED_URL",
        "SPINANDBET_AUTH_HEADER",
        "SPINANDBET_AUTH_VALUE",
    )

    return quotes


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
        "time": datetime.now(timezone.utc).isoformat(),
    })


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
