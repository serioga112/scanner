import os
import json
import time
import uuid
import base64
import zlib
from datetime import datetime, timezone

import requests
import websocket
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
        "spinWsUrl": SPIN_WS_URL,
        "spinOrigin": SPIN_ORIGIN,
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
# SpinAndBet / BetConstruct Swarm live market loader
# ---------------------------------------------------------------------------

def _rid():
    return uuid.uuid4().hex[:12]


def _safe_json_loads(value):
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _decode_binary_payload(raw):
    if not isinstance(raw, (bytes, bytearray)):
        return {"kind": "not-binary"}

    raw = bytes(raw)
    out = {
        "kind": "binary",
        "length": len(raw),
        "hexPreview": raw[:96].hex(),
        "base64Preview": base64.b64encode(raw[:192]).decode("ascii"),
    }

    try:
        text = raw.decode("utf-8")
        out["utf8"] = text[:12000]
        parsed = _safe_json_loads(text)
        if parsed is not None:
            out["json"] = parsed
            out["decodedBy"] = "utf8-json"
            return out
    except UnicodeDecodeError:
        pass

    attempts = [
        ("zlib", zlib.MAX_WBITS),
        ("raw-deflate", -zlib.MAX_WBITS),
        ("gzip", zlib.MAX_WBITS | 16),
    ]
    candidates = [raw, raw + b"\x00\x00\xff\xff"]

    for candidate in candidates:
        for label, wbits in attempts:
            try:
                dec = zlib.decompress(candidate, wbits)
            except Exception:
                continue
            try:
                text = dec.decode("utf-8")
            except UnicodeDecodeError:
                continue
            out["decodedBy"] = label
            out["decodedText"] = text[:12000]
            parsed = _safe_json_loads(text)
            if parsed is not None:
                out["json"] = parsed
            return out

    return out


def _compact_frame(message, index):
    if isinstance(message, str):
        parsed = _safe_json_loads(message)
        return {
            "index": index,
            "direction": "server_to_client",
            "type": "text",
            "length": len(message.encode("utf-8")),
            "json": parsed,
            "text": message[:12000] if parsed is None else None,
        }

    if isinstance(message, (bytes, bytearray)):
        decoded = _decode_binary_payload(message)
        decoded.update({
            "index": index,
            "direction": "server_to_client",
            "type": "binary",
        })
        return decoded

    return {
        "index": index,
        "direction": "server_to_client",
        "type": type(message).__name__,
        "repr": repr(message)[:2000],
    }


def _request_session_message():
    params = {
        "language": SPIN_LANGUAGE,
        "site_id": SPIN_SITE_ID,
        "source": SPIN_SOURCE,
        "release_date": SPIN_RELEASE_DATE,
    }
    if SPIN_AFEC:
        params["afec"] = SPIN_AFEC
    return {"command": "request_session", "params": params, "rid": _rid()}


def _soccer_snapshot_message(subscribe=False):
    # This uses the documented Swarm hierarchy and restricts the first pass to
    # soccer 1X2 markets so the payload stays small enough for reliable testing.
    return {
        "command": "get",
        "params": {
            "source": "betting",
            "what": {
                "sport": ["name"],
                "region": ["name"],
                "competition": ["name", "id"],
                "game": [
                    "id", "start_ts", "type", "team1_id", "team1_name",
                    "team2_id", "team2_name", "sport_alias", "region_alias",
                    "is_blocked", "markets_count"
                ],
                "market": [
                    "id", "type", "name", "name_template", "base",
                    "group_name", "display_key"
                ],
                "event": [
                    "id", "type_1", "price", "name", "base", "home_value",
                    "away_value"
                ],
            },
            "where": {
                "sport": {"alias": SPIN_SPORT_ALIAS},
                "game": {"type": {"@in": [0, 2]}},
                "market": {"type": {"@in": ["P1XP2"]}},
            },
            "subscribe": bool(subscribe),
        },
        "rid": _rid(),
    }


def _open_spin_socket():
    """
    Try a small set of normal/public browser-style handshake profiles.

    No account cookies, authorization tokens, CAPTCHA bypass, proxying,
    or Cloudflare-challenge circumvention is used.
    """
    profiles = [
        {
            "name": "configured-origin",
            "origin": SPIN_ORIGIN,
            "suppress_origin": False,
        },
        {
            "name": "www-origin",
            "origin": "https://www.spinandbet.net",
            "suppress_origin": False,
        },
        {
            "name": "root-origin",
            "origin": "https://spinandbet.net",
            "suppress_origin": False,
        },
        {
            "name": "no-origin",
            "origin": None,
            "suppress_origin": True,
        },
    ]

    errors = []

    for profile in profiles:
        kwargs = {
            "timeout": SPIN_WS_TIMEOUT,
            "header": [
                "Cache-Control: no-cache",
                "Pragma: no-cache",
                "Accept-Language: en-US,en;q=0.9",
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/139.0.0.0 Safari/537.36",
            ],
            "enable_multithread": False,
        }

        if profile["suppress_origin"]:
            kwargs["suppress_origin"] = True
        else:
            kwargs["origin"] = profile["origin"]

        try:
            ws = websocket.create_connection(SPIN_WS_URL, **kwargs)
            ws._spin_handshake_profile = profile["name"]
            return ws
        except Exception as exc:
            errors.append(
                f'{profile["name"]}: {type(exc).__name__}: {exc}'
            )

    raise RuntimeError(
        "All public WebSocket handshake profiles failed. " + " | ".join(errors)
    )


def _recv_until_rid(ws, wanted_rid, frames, max_messages=None, timeout=None):
    max_messages = max_messages or SPIN_WS_MAX_MESSAGES
    timeout = timeout or SPIN_WS_TIMEOUT
    deadline = time.time() + timeout
    response = None

    while len(frames) < max_messages and time.time() < deadline:
        try:
            msg = ws.recv()
        except websocket.WebSocketTimeoutException:
            break

        frame = _compact_frame(msg, len(frames))
        frames.append(frame)
        obj = frame.get("json")
        if isinstance(obj, dict) and str(obj.get("rid")) == str(wanted_rid):
            response = obj
            break

    return response


def _entity_items(value):
    if isinstance(value, dict):
        return value.items()
    if isinstance(value, list):
        return ((str(i), item) for i, item in enumerate(value))
    return ()


def parse_spinandbet_markets(response_obj):
    """Convert a Swarm get response into the same market shape used by Stake."""
    if not isinstance(response_obj, dict):
        return []

    root = response_obj.get("data", response_obj)
    if isinstance(root, dict) and "data" in root and isinstance(root["data"], (dict, list)):
        root = root["data"]

    markets = {}

    def walk(node, ctx):
        if isinstance(node, list):
            for item in node:
                walk(item, ctx)
            return
        if not isinstance(node, dict):
            return

        for key, value in node.items():
            if key == "sport":
                for entity_id, entity in _entity_items(value):
                    if isinstance(entity, dict):
                        c = dict(ctx)
                        c["sport"] = entity.get("name") or entity.get("alias") or c.get("sport")
                        walk(entity, c)
                continue

            if key == "region":
                for entity_id, entity in _entity_items(value):
                    if isinstance(entity, dict):
                        c = dict(ctx)
                        c["region"] = entity.get("name") or entity.get("alias") or c.get("region")
                        walk(entity, c)
                continue

            if key == "competition":
                for entity_id, entity in _entity_items(value):
                    if isinstance(entity, dict):
                        c = dict(ctx)
                        c["competitionId"] = str(entity.get("id") or entity_id)
                        c["competition"] = entity.get("name") or c.get("competition")
                        walk(entity, c)
                continue

            if key == "game":
                for entity_id, entity in _entity_items(value):
                    if isinstance(entity, dict):
                        c = dict(ctx)
                        c["gameId"] = str(entity.get("id") or entity_id)
                        c["team1"] = str(entity.get("team1_name") or "").strip()
                        c["team2"] = str(entity.get("team2_name") or "").strip()
                        c["startTime"] = entity.get("start_ts")
                        c["sport"] = entity.get("sport_alias") or c.get("sport")
                        c["region"] = entity.get("region_alias") or c.get("region")
                        if c["team1"] or c["team2"]:
                            c["event"] = f'{c["team1"]} - {c["team2"]}'.strip(" -")
                        walk(entity, c)
                continue

            if key == "market":
                for entity_id, entity in _entity_items(value):
                    if isinstance(entity, dict):
                        c = dict(ctx)
                        c["marketId"] = str(entity.get("id") or entity_id)
                        c["market"] = str(
                            entity.get("name") or entity.get("name_template") or
                            entity.get("type") or "market"
                        ).strip()
                        c["marketType"] = str(entity.get("type") or "").strip()
                        c["marketBase"] = entity.get("base")
                        walk(entity, c)
                continue

            if key == "event":
                for entity_id, entity in _entity_items(value):
                    if not isinstance(entity, dict):
                        continue
                    try:
                        price = float(entity.get("price"))
                    except (TypeError, ValueError):
                        continue
                    if price <= 1:
                        continue

                    game_id = ctx.get("gameId", "")
                    market_id = ctx.get("marketId", "")
                    if not game_id or not market_id:
                        continue

                    mk = (game_id, market_id)
                    if mk not in markets:
                        markets[mk] = {
                            "bookmaker": "SpinAndBet",
                            "eventId": game_id,
                            "event": ctx.get("event") or game_id,
                            "team1": ctx.get("team1", ""),
                            "team2": ctx.get("team2", ""),
                            "startTime": ctx.get("startTime"),
                            "sport": ctx.get("sport") or SPIN_SPORT_ALIAS,
                            "region": ctx.get("region"),
                            "tournament": ctx.get("competition"),
                            "competitionId": ctx.get("competitionId"),
                            "marketId": market_id,
                            "market": ctx.get("market") or ctx.get("marketType") or "market",
                            "marketType": ctx.get("marketType"),
                            "base": ctx.get("marketBase"),
                            "outcomes": [],
                        }

                    markets[mk]["outcomes"].append({
                        "selection": str(
                            entity.get("name") or entity.get("type_1") or entity_id
                        ).strip(),
                        "type": str(entity.get("type_1") or "").strip(),
                        "odds": price,
                        "eventId": str(entity.get("id") or entity_id),
                        "base": entity.get("base"),
                    })
                continue

            walk(value, ctx)

    walk(root, {})
    return [m for m in markets.values() if len(m["outcomes"]) >= 2]


def fetch_spinandbet_markets():
    frames = []
    sent = []
    ws = None
    try:
        ws = _open_spin_socket()

        session_msg = _request_session_message()
        ws.send(json.dumps(session_msg, separators=(",", ":")))
        sent.append({"kind": "request_session", "rid": session_msg["rid"]})
        session_response = _recv_until_rid(ws, session_msg["rid"], frames)

        if not isinstance(session_response, dict):
            return {
                "ok": False, "stage": "request_session", "markets": [],
                "frames": frames, "sent": sent,
                "errors": ["No request_session response received before timeout"],
            }
        if session_response.get("code") != 0:
            return {
                "ok": False, "stage": "request_session", "markets": [],
                "frames": frames, "sent": sent,
                "errors": [f'Swarm request_session failed: {session_response.get("msg") or session_response}'],
                "sessionResponse": session_response,
            }

        snapshot_msg = _soccer_snapshot_message(subscribe=False)
        ws.send(json.dumps(snapshot_msg, separators=(",", ":")))
        sent.append({"kind": "soccer_1x2_snapshot", "rid": snapshot_msg["rid"]})
        snapshot_response = _recv_until_rid(ws, snapshot_msg["rid"], frames)

        if not isinstance(snapshot_response, dict):
            return {
                "ok": False, "stage": "get", "markets": [],
                "frames": frames, "sent": sent,
                "errors": ["Session succeeded, but no get response was received before timeout"],
                "sessionResponse": session_response,
            }
        if snapshot_response.get("code") != 0:
            return {
                "ok": False, "stage": "get", "markets": [],
                "frames": frames, "sent": sent,
                "errors": [f'Swarm get failed: {snapshot_response.get("msg") or snapshot_response}'],
                "sessionResponse": session_response,
                "snapshotResponse": snapshot_response,
            }

        markets = parse_spinandbet_markets(snapshot_response)
        return {
            "ok": True,
            "stage": "complete",
            "markets": markets,
            "frames": frames,
            "sent": sent,
            "errors": [],
            "sessionResponse": session_response,
            "snapshotResponse": snapshot_response,
        }

    except Exception as exc:
        return {
            "ok": False, "stage": "exception", "markets": [],
            "frames": frames, "sent": sent,
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


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


@app.get("/api/spinandbet-debug")
def spinandbet_debug():
    data = fetch_spinandbet_markets()
    return jsonify({
        "ok": data["ok"],
        "stage": data.get("stage"),
        "wsUrl": SPIN_WS_URL,
        "origin": SPIN_ORIGIN,
        "siteId": SPIN_SITE_ID,
        "source": SPIN_SOURCE,
        "afecConfigured": bool(SPIN_AFEC),
        "marketsLoaded": len(data.get("markets", [])),
        "sampleMarkets": data.get("markets", [])[:10],
        "sent": data.get("sent", []),
        "frameCount": len(data.get("frames", [])),
        "frames": data.get("frames", [])[:10],
        "errors": data.get("errors", []),
    })


@app.get("/api/spinandbet-markets")
def spinandbet_markets():
    data = fetch_spinandbet_markets()
    return jsonify({
        "ok": data["ok"],
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
    spin_data = fetch_spinandbet_markets()
    combined = stake_data.get("markets", []) + spin_data.get("markets", [])
    arbs = find_1x2_arbitrage(combined, bankroll, minimum)

    return jsonify({
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "bankroll": bankroll,
        "minArbPercent": minimum,
        "stakeMarketsLoaded": len(stake_data.get("markets", [])),
        "spinAndBetMarketsLoaded": len(spin_data.get("markets", [])),
        "opportunities": arbs,
        "errors": {
            "stake": stake_data.get("errors", []),
            "spinAndBet": spin_data.get("errors", []),
        },
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
