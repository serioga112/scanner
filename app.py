import os
import json
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

# SpinAndBet / BetConstruct Swarm WebSocket.
# These defaults come from the public, unauthenticated sportsbook traffic we identified.
SPIN_WS_URL = os.getenv("SPIN_WS_URL", "wss://eu-swarm-newm.spinandbet.net/").strip()
SPIN_HTTP_URL = os.getenv(
    "SPIN_HTTP_URL",
    SPIN_WS_URL.replace("wss://", "https://", 1).replace("ws://", "http://", 1),
).strip()
SPIN_ORIGIN = os.getenv("SPIN_ORIGIN", "https://m.spinandbet.net").strip()
SPIN_SITE_ID = int(os.getenv("SPIN_SITE_ID", "1033"))
SPIN_SOURCE = int(os.getenv("SPIN_SOURCE", "6"))
SPIN_LANGUAGE = os.getenv("SPIN_LANGUAGE", "eng").strip()
SPIN_RELEASE_DATE = os.getenv("SPIN_RELEASE_DATE", "08/18/2026-14:55").strip()
# Optional public-page client parameter seen in request_session.
# Do not put cookies, account passwords, authorization headers, or logged-in session IDs here.
SPIN_AFEC = os.getenv("SPIN_AFEC", "").strip()

SPIN_EVENT_ID = int(os.getenv("SPIN_EVENT_ID", "30086420"))
SPIN_COMPETITION_ID = int(os.getenv("SPIN_COMPETITION_ID", "538"))
SPIN_SPORT_ALIAS = os.getenv("SPIN_SPORT_ALIAS", "Soccer").strip()
SPIN_REGION_ALIAS = os.getenv("SPIN_REGION_ALIAS", "England").strip()
SPIN_WS_TIMEOUT = float(os.getenv("SPIN_WS_TIMEOUT", "8"))
SPIN_WS_MAX_MESSAGES = int(os.getenv("SPIN_WS_MAX_MESSAGES", "30"))


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


# ---------------------------------------------------------------------------
# Shuffle GraphQL sportsbook loader
# ---------------------------------------------------------------------------

SHUFFLE_GRAPHQL_URL = os.getenv(
    "SHUFFLE_GRAPHQL_URL", "https://shuffle.com/main-api/graphql/api/graphql"
).strip()
SHUFFLE_ORIGIN = os.getenv("SHUFFLE_ORIGIN", "https://shuffle.com").strip()
# Paste the public sportsbook GraphQL POST body captured from Shuffle into this
# environment variable. Do not include account cookies or Authorization tokens.
SHUFFLE_GRAPHQL_BODY = os.getenv("SHUFFLE_GRAPHQL_BODY", "").strip()


def _shuffle_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": SHUFFLE_ORIGIN,
        "Referer": SHUFFLE_ORIGIN.rstrip("/") + "/sports",
        "User-Agent": "Mozilla/5.0",
    }


def _shuffle_event_name(node, ctx):
    for key in ("eventName", "fixtureName", "matchName", "name", "title"):
        value = node.get(key)
        if isinstance(value, str) and (" vs " in value.lower() or " v " in value.lower() or " - " in value):
            return value.strip()
    home = node.get("homeTeam") or node.get("home") or node.get("competitor1")
    away = node.get("awayTeam") or node.get("away") or node.get("competitor2")
    def label(v):
        if isinstance(v, dict):
            return str(v.get("name") or v.get("label") or "").strip()
        return str(v or "").strip()
    h, a = label(home), label(away)
    if h and a:
        return f"{h} - {a}"
    return ctx.get("event", "")


def _shuffle_odds(outcome):
    for key in ("odds", "price", "decimalOdds", "decimal", "value"):
        try:
            value = float(outcome.get(key))
            if value > 1:
                return value
        except (TypeError, ValueError):
            pass
    return None


def parse_shuffle_markets(payload):
    """Best-effort parser for Shuffle GraphQL sportsbook responses.

    It recursively discovers market-like objects containing an outcomes/selections
    list, then normalizes them to the same shape as the Stake loader. Keeping the
    parser schema-tolerant means only the GraphQL request body normally needs to
    be updated when Shuffle changes its frontend query.
    """
    markets = []
    seen = set()

    def walk(node, ctx):
        if isinstance(node, list):
            for item in node:
                walk(item, ctx)
            return
        if not isinstance(node, dict):
            return

        c = dict(ctx)
        event = _shuffle_event_name(node, c)
        if event:
            c["event"] = event
        for key in ("eventId", "fixtureId", "matchId", "id"):
            if node.get(key) is not None and c.get("event"):
                c.setdefault("eventId", str(node.get(key)))
                break
        c["sport"] = str(node.get("sportName") or node.get("sport") or c.get("sport") or "football")
        c["tournament"] = node.get("leagueName") or node.get("tournamentName") or node.get("competitionName") or c.get("tournament")
        c["startTime"] = node.get("startTime") or node.get("startDate") or node.get("startsAt") or c.get("startTime")

        outcome_list = None
        for key in ("outcomes", "selections", "runners", "options"):
            if isinstance(node.get(key), list):
                outcome_list = node[key]
                break

        if outcome_list:
            outcomes = []
            for outcome in outcome_list:
                if not isinstance(outcome, dict):
                    continue
                odds = _shuffle_odds(outcome)
                if not odds:
                    continue
                selection = str(
                    outcome.get("name") or outcome.get("label") or outcome.get("title")
                    or outcome.get("selectionName") or outcome.get("type") or ""
                ).strip()
                if selection:
                    outcomes.append({"selection": selection, "odds": odds})

            if len(outcomes) in (2, 3) and c.get("event"):
                market_name = str(
                    node.get("marketName") or node.get("name") or node.get("title")
                    or node.get("type") or "market"
                ).strip()
                sig = (c["event"].lower(), market_name.lower(), tuple((o["selection"].lower(), o["odds"]) for o in outcomes))
                if sig not in seen:
                    seen.add(sig)
                    markets.append({
                        "bookmaker": "Shuffle",
                        "eventId": c.get("eventId", ""),
                        "event": c["event"],
                        "startTime": c.get("startTime"),
                        "sport": c.get("sport", "football"),
                        "tournament": c.get("tournament"),
                        "market": market_name,
                        "marketType": node.get("marketType") or node.get("type") or market_name,
                        "outcomes": outcomes,
                    })

        for value in node.values():
            walk(value, c)

    walk(payload.get("data", payload) if isinstance(payload, dict) else payload, {})
    return markets


def fetch_shuffle_markets():
    if not SHUFFLE_GRAPHQL_BODY:
        return {
            "ok": False,
            "stage": "configuration",
            "markets": [],
            "errors": ["SHUFFLE_GRAPHQL_BODY is not configured. Capture the public sports GraphQL request body and add it in Render Environment."],
        }
    try:
        body = json.loads(SHUFFLE_GRAPHQL_BODY)
    except Exception as exc:
        return {"ok": False, "stage": "configuration", "markets": [], "errors": [f"Invalid SHUFFLE_GRAPHQL_BODY JSON: {exc}"]}

    try:
        response = requests.post(
            SHUFFLE_GRAPHQL_URL,
            headers=_shuffle_headers(),
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        gql_errors = payload.get("errors", []) if isinstance(payload, dict) else []
        markets = parse_shuffle_markets(payload)
        return {
            "ok": not gql_errors,
            "stage": "complete",
            "markets": markets,
            "errors": gql_errors,
            "responseKeys": list(payload.keys()) if isinstance(payload, dict) else [],
        }
    except Exception as exc:
        return {"ok": False, "stage": "request", "markets": [], "errors": [f"{type(exc).__name__}: {exc}"]}


def _norm_text(value):
    import re
    value = str(value or "").lower().strip()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _event_teams(market):
    t1 = _norm_text(market.get("team1"))
    t2 = _norm_text(market.get("team2"))
    if t1 and t2:
        return t1, t2

    event = str(market.get("event") or "")
    import re
    parts = re.split(r"\s+(?:vs?\.?|[-–—])\s+", event, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return _norm_text(parts[0]), _norm_text(parts[1])
    return _norm_text(event), ""


def _canonical_market_type(market):
    raw = _norm_text(market.get("marketType") or market.get("market"))
    compact = raw.replace(" ", "")
    if compact in {"p1xp2", "1x2"} or "match result" in raw or "full time result" in raw:
        return "1x2"
    return raw


def _canonical_outcome(market, outcome):
    raw = _norm_text(outcome.get("type") or outcome.get("selection") or outcome.get("name"))
    compact = raw.replace(" ", "")
    if compact in {"p1", "1", "home", "team1", "w1"}:
        return "1"
    if compact in {"x", "draw", "tie"}:
        return "X"
    if compact in {"p2", "2", "away", "team2", "w2"}:
        return "2"

    t1, t2 = _event_teams(market)
    if raw and raw == t1:
        return "1"
    if raw and raw == t2:
        return "2"
    return None


def find_1x2_arbitrage(markets, bankroll, min_arb_percent):
    groups = {}
    for market in markets:
        if _canonical_market_type(market) != "1x2":
            continue
        teams = _event_teams(market)
        if not teams[0] or not teams[1]:
            continue
        groups.setdefault(teams, []).append(market)

    results = []
    for teams, group in groups.items():
        if len({m.get("bookmaker") for m in group}) < 2:
            continue

        best = {}
        for market in group:
            for outcome in market.get("outcomes", []):
                label = _canonical_outcome(market, outcome)
                if label not in {"1", "X", "2"}:
                    continue
                try:
                    odds = float(outcome.get("odds"))
                except (TypeError, ValueError):
                    continue
                candidate = {
                    "bookmaker": market.get("bookmaker"),
                    "selection": outcome.get("selection") or outcome.get("name") or label,
                    "canonicalSelection": label,
                    "odds": odds,
                }
                if label not in best or odds > best[label]["odds"]:
                    best[label] = candidate

        if set(best) != {"1", "X", "2"}:
            continue

        arb_sum = sum(1.0 / best[x]["odds"] for x in ("1", "X", "2"))
        arb_percent = (1.0 - arb_sum) * 100.0
        if arb_sum >= 1.0 or arb_percent < min_arb_percent:
            continue

        guaranteed_return = bankroll / arb_sum
        legs = []
        for label in ("1", "X", "2"):
            item = dict(best[label])
            item["stake"] = round(bankroll * (1.0 / item["odds"]) / arb_sum, 2)
            legs.append(item)

        results.append({
            "event": f"{teams[0]} - {teams[1]}",
            "market": "1X2",
            "arbPercent": round(arb_percent, 4),
            "impliedProbabilitySum": round(arb_sum, 6),
            "bankroll": bankroll,
            "guaranteedReturn": round(guaranteed_return, 2),
            "guaranteedProfit": round(guaranteed_return - bankroll, 2),
            "legs": legs,
        })

    return sorted(results, key=lambda x: x["arbPercent"], reverse=True)


@app.get("/api/shuffle-debug")
def shuffle_debug():
    data = fetch_shuffle_markets()
    return jsonify({
        "ok": data.get("ok", False),
        "stage": data.get("stage"),
        "graphqlUrl": SHUFFLE_GRAPHQL_URL,
        "bodyConfigured": bool(SHUFFLE_GRAPHQL_BODY),
        "marketsLoaded": len(data.get("markets", [])),
        "sampleMarkets": data.get("markets", [])[:10],
        "errors": data.get("errors", []),
    })


@app.get("/api/shuffle-markets")
def shuffle_markets():
    data = fetch_shuffle_markets()
    return jsonify({
        "ok": data.get("ok", False),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "marketsLoaded": len(data.get("markets", [])),
        "quotesLoaded": sum(len(m.get("outcomes", [])) for m in data.get("markets", [])),
        "markets": data.get("markets", []),
        "errors": data.get("errors", []),
    })


@app.get("/api/opportunities")
def opportunities():
    bankroll = float(request.args.get("bankroll", os.getenv("BANKROLL", "1000")))
    minimum = float(request.args.get("min_arb_percent", os.getenv("MIN_ARB_PERCENT", "0.5")))

    stake_data = fetch_stake_markets()
    shuffle_data = fetch_shuffle_markets()
    combined = stake_data.get("markets", []) + shuffle_data.get("markets", [])
    arbs = find_1x2_arbitrage(combined, bankroll, minimum)

    return jsonify({
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "bankroll": bankroll,
        "minArbPercent": minimum,
        "stakeMarketsLoaded": len(stake_data.get("markets", [])),
        "shuffleMarketsLoaded": len(shuffle_data.get("markets", [])),
        "opportunities": arbs,
        "errors": {
            "stake": stake_data.get("errors", []),
            "shuffle": shuffle_data.get("errors", []),
        },
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
