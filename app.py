import os
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=None)

SCANNER_VERSION = "v10-closest-twoway"

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
                    # Preserve line/type metadata when Stake attaches the total or
                    # handicap to each outcome rather than the parent market node.
                    usable.append({
                        "selection": selection,
                        "odds": odds,
                        "type": outcome.get("type"),
                        "lineValue": outcome.get("lineValue"),
                        "line": outcome.get("line"),
                        "handicap": outcome.get("handicap"),
                        "points": outcome.get("points"),
                        "base": outcome.get("base"),
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
            "marketType": node.get("marketType") or node.get("type") or node.get("displayType"),
            "lineValue": (
                node.get("lineValue") if node.get("lineValue") is not None else
                node.get("line") if node.get("line") is not None else
                node.get("base") if node.get("base") is not None else
                node.get("handicap") if node.get("handicap") is not None else
                node.get("points")
            ),
            "jsonPath": item["path"],
            "outcomes": outcomes,
        })

    return parsed


def _flatten_stake_schedule(payload):
    """Flatten GET /schedule/sport/{sport} into fixture dictionaries."""
    fixtures = []
    seen = set()

    if not isinstance(payload, dict):
        return fixtures

    schedule = payload.get("schedule")
    if not isinstance(schedule, list):
        return fixtures

    for bucket in schedule:
        if not isinstance(bucket, dict):
            continue
        bucket_date = bucket.get("date")
        rows = bucket.get("fixture") or bucket.get("fixtures") or []
        if not isinstance(rows, list):
            continue

        for fixture in rows:
            if not isinstance(fixture, dict):
                continue
            item = dict(fixture)
            if item.get("date") is None and item.get("startTime") is None:
                item["date"] = bucket_date
            key = str(item.get("slug") or item.get("id") or item.get("extId") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            fixtures.append(item)

    return fixtures


def _is_virtual_stake_fixture(fixture):
    """Reject simulated/virtual football that must not match real Shuffle soccer."""
    if not isinstance(fixture, dict):
        return True

    haystack = " ".join(str(fixture.get(k) or "") for k in (
        "name", "slug", "tournament", "category", "type"
    )).lower()

    markers = (
        " srl", "srl ", "-srl", "srl-",
        "simulated reality", "simulated",
        "esoccer", "e-soccer", "e soccer",
        "cyber", "fifa bots", "fifa bot",
    )
    return any(marker in haystack for marker in markers)


def fetch_stake_markets():
    if not STAKE_API_KEY:
        return {
            "ok": False,
            "fixtures": [],
            "markets": [],
            "errors": ["STAKE_API_KEY is not configured"],
            "fixtureSource": None,
            "rawFixturesDiscovered": 0,
            "virtualFixturesFiltered": 0,
        }

    errors = []
    fixtures_meta = []
    markets = []
    fixture_source = "schedule"

    # The simple /sport/{sport}/fixture endpoint can return only a small
    # featured slice. The schedule endpoint exposes the broader upcoming list.
    try:
        schedule_payload = stake_get(f"/schedule/sport/{STAKE_SPORT}")
        fixtures = _flatten_stake_schedule(schedule_payload)
        if not fixtures:
            raise ValueError("schedule response contained no fixtures")
    except Exception as schedule_exc:
        fixture_source = "sport-fixture-fallback"
        try:
            fixture_list_payload = stake_get(f"/sport/{STAKE_SPORT}/fixture")
            fixtures = extract_fixture_list(fixture_list_payload)
            errors.append(
                f"schedule-fallback: {type(schedule_exc).__name__}: {schedule_exc}"
            )
        except Exception as exc:
            return {
                "ok": False,
                "fixtures": [],
                "markets": [],
                "errors": [
                    f"schedule: {type(schedule_exc).__name__}: {schedule_exc}",
                    f"fixture-list: {type(exc).__name__}: {exc}",
                ],
                "fixtureSource": None,
                "rawFixturesDiscovered": 0,
                "virtualFixturesFiltered": 0,
            }

    raw_count = len(fixtures)
    real_fixtures = [f for f in fixtures if not _is_virtual_stake_fixture(f)]
    virtual_filtered = raw_count - len(real_fixtures)

    # Sort chronologically when Stake provides timestamps, then cap API work.
    def fixture_sort_key(fixture):
        value = fixture.get("startTime") or fixture.get("date")
        try:
            n = float(value)
            if n > 10_000_000_000:
                n /= 1000.0
            return n
        except (TypeError, ValueError):
            return float("inf")

    real_fixtures.sort(key=fixture_sort_key)

    for fixture in real_fixtures[:STAKE_FIXTURE_LIMIT]:
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
        "ok": len([e for e in errors if not e.startswith("schedule-fallback:")]) == 0,
        "fixtures": fixtures_meta,
        "markets": markets,
        "errors": errors,
        "fixtureSource": fixture_source,
        "rawFixturesDiscovered": raw_count,
        "virtualFixturesFiltered": virtual_filtered,
    }


@app.get("/")
def home():
    root = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(root, "index.html")


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "version": SCANNER_VERSION,
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
        "fixtureSource": data.get("fixtureSource"),
        "rawFixturesDiscovered": data.get("rawFixturesDiscovered"),
        "virtualFixturesFiltered": data.get("virtualFixturesFiltered"),
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
    "SHUFFLE_GRAPHQL_URL", "https://shuffle.com/main-api/graphql/sports/graphql-sports"
).strip()
SHUFFLE_ORIGIN = os.getenv("SHUFFLE_ORIGIN", "https://shuffle.com").strip()
# Paste the public sportsbook GraphQL POST body captured from Shuffle into this
# environment variable. Do not include account cookies or Authorization tokens.
SHUFFLE_GRAPHQL_BODY = os.getenv("SHUFFLE_GRAPHQL_BODY", "").strip()

# v6: use Shuffle's own Upcoming Soccer feed by default. The HAR captured from
# the public sportsbook shows that the Upcoming tab calls GetSportsFixtures
# with searchType=UPCOMING_ONLY and paginates with nextCursor.
SHUFFLE_SCAN_MODE = os.getenv("SHUFFLE_SCAN_MODE", "UPCOMING").strip().upper()
SHUFFLE_FIXTURE_PAGE_SIZE = int(os.getenv("SHUFFLE_FIXTURE_PAGE_SIZE", "25"))
SHUFFLE_MAX_PAGES = int(os.getenv("SHUFFLE_MAX_PAGES", "10"))

SHUFFLE_UPCOMING_QUERY = """query GetSportsFixtures($first: Int, $cursor: String, $sports: Sports, $categoryId: String, $competitionId: String, $searchType: SportsSearchType!, $language: Language, $prioritizedMarketTypeId: String) {
  sportsFixtures: sportsFixturesV2(first: $first, cursor: $cursor, sports: $sports, categoryId: $categoryId, competitionId: $competitionId, searchType: $searchType, language: $language, prioritizedMarketTypeId: $prioritizedMarketTypeId)
}"""


def _shuffle_upcoming_body(cursor=None):
    variables = {
        "first": SHUFFLE_FIXTURE_PAGE_SIZE,
        "language": "en",
        "searchType": "UPCOMING_ONLY",
        "sports": "SOCCER",
    }
    if cursor:
        variables["cursor"] = cursor
    return {
        "operationName": "GetSportsFixtures",
        "variables": variables,
        "extensions": {
            "clientLibrary": {
                "name": "@apollo/client",
                "version": "4.1.6",
            }
        },
        "query": SHUFFLE_UPCOMING_QUERY,
    }


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

    competitors = node.get("competitors")
    if isinstance(competitors, list):
        names = [
            str(x.get("displayName") or x.get("name") or "").strip()
            for x in competitors if isinstance(x, dict)
        ]
        names = [x for x in names if x]
        if len(names) >= 2:
            return f"{names[0]} - {names[1]}"

    home = node.get("homeTeam") or node.get("home") or node.get("competitor1")
    away = node.get("awayTeam") or node.get("away") or node.get("competitor2")

    def label(v):
        if isinstance(v, dict):
            return str(v.get("name") or v.get("displayName") or v.get("label") or "").strip()
        return str(v or "").strip()

    h, a = label(home), label(away)
    if h and a:
        return f"{h} - {a}"
    return ctx.get("event", "")


def _shuffle_odds(outcome):
    """Return decimal odds from Shuffle's sportsbook selection object.

    Shuffle's sports GraphQL currently exposes fractional odds as strings such
    as oddsNumerator="160", oddsDenominator="100". Those represent 160/100
    fractional odds, i.e. decimal odds 2.60.
    """
    if not isinstance(outcome, dict):
        return None

    numerator = outcome.get("oddsNumerator")
    denominator = outcome.get("oddsDenominator")
    try:
        n = float(numerator)
        d = float(denominator)
        if d > 0:
            value = 1.0 + (n / d)
            if value > 1.0:
                return value
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    for key in ("odds", "price", "decimalOdds", "decimal", "value"):
        try:
            value = float(outcome.get(key))
            if value > 1:
                return value
        except (TypeError, ValueError):
            pass
    return None


def _shuffle_competitor_names(fixture):
    home = ""
    away = ""
    ordered = []
    for item in fixture.get("competitors", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("displayName") or item.get("name") or "").strip()
        if not name:
            continue
        ordered.append(name)
        if item.get("isHome") is True:
            home = name
        elif item.get("isHome") is False:
            away = name
    if not home and ordered:
        home = ordered[0]
    if not away and len(ordered) > 1:
        away = ordered[1]
    return home, away


def _shuffle_selection_labels(display):
    """Map Shuffle selection IDs/provider IDs to their human-readable labels."""
    mapping = {}
    if not isinstance(display, dict):
        return mapping
    for group in display.get("selectionGroups", []) or []:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or "").strip()
        for selection in group.get("selections", []) or []:
            if not isinstance(selection, dict):
                continue
            label = str(
                selection.get("fullName")
                or selection.get("name")
                or group_name
                or ""
            ).strip()
            if not label:
                continue
            for key in ("id", "providerId"):
                value = selection.get(key)
                if value:
                    mapping[str(value)] = label
    return mapping


def _shuffle_market_type(group_name, outcomes):
    raw = str(group_name or "").strip()
    norm = _norm_text(raw)
    labels = {_norm_text(x.get("selection")) for x in outcomes}
    if len(outcomes) == 3 and ("draw" in labels or "x" in labels):
        return "1X2"
    if "threeway" in norm or "three way" in norm or "match result" in norm or "full time result" in norm:
        return "1X2"
    if "twoway" in norm or "two way" in norm:
        return "2WAY"
    return raw or "market"


def _parse_shuffle_market_bundle(bundle, fixture_ctx, path, markets, seen):
    if not isinstance(bundle, dict):
        return

    display = bundle.get("display") if isinstance(bundle.get("display"), dict) else {}
    market_name = str(display.get("groupName") or display.get("groupKey") or "market").strip()
    label_map = _shuffle_selection_labels(display)

    odds_groups = bundle.get("odds")
    if not isinstance(odds_groups, list):
        return

    for odds_index, odds_group in enumerate(odds_groups):
        if not isinstance(odds_group, dict):
            continue
        if str(odds_group.get("status") or "OPEN").upper() not in {"OPEN", "ACTIVE", "TRADING"}:
            continue

        outcomes = []
        for raw in odds_group.get("selections", []) or []:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("status") or "TRADING").upper() not in {"TRADING", "OPEN", "ACTIVE"}:
                continue
            odds = _shuffle_odds(raw)
            if not odds:
                continue
            label = (
                label_map.get(str(raw.get("id") or ""))
                or label_map.get(str(raw.get("providerId") or ""))
                or str(raw.get("name") or raw.get("label") or "").strip()
            )
            if not label:
                continue
            outcomes.append({
                "selection": label,
                "odds": round(odds, 6),
                "selectionId": str(raw.get("id") or ""),
                "providerId": str(raw.get("providerId") or ""),
            })

        if len(outcomes) not in (2, 3):
            continue

        market_type = _shuffle_market_type(market_name, outcomes)
        event = fixture_ctx.get("event", "")
        sig = (
            fixture_ctx.get("eventId") or event.lower(),
            _norm_text(market_name),
            tuple(sorted((x["selection"].lower(), x["odds"]) for x in outcomes)),
        )
        if sig in seen:
            continue
        seen.add(sig)

        markets.append({
            "bookmaker": "Shuffle",
            "eventId": fixture_ctx.get("eventId", ""),
            "event": event,
            "team1": fixture_ctx.get("team1", ""),
            "team2": fixture_ctx.get("team2", ""),
            "startTime": fixture_ctx.get("startTime"),
            "status": fixture_ctx.get("status"),
            "inPlay": bool(odds_group.get("inPlay")),
            "sport": fixture_ctx.get("sport") or "football",
            "tournament": fixture_ctx.get("tournament"),
            "market": market_name,
            "marketType": market_type,
            "groupKey": display.get("groupKey"),
            "displayType": display.get("displayType") or display.get("marketDisplayType"),
            "lineValue": (
                odds_group.get("lineValue") if odds_group.get("lineValue") is not None else
                odds_group.get("line") if odds_group.get("line") is not None else
                odds_group.get("base") if odds_group.get("base") is not None else
                display.get("lineValue")
            ),
            "jsonPath": f"{path}.odds[{odds_index}]",
            "outcomes": outcomes,
        })


def parse_shuffle_markets(payload):
    """Normalize the current Shuffle sports GraphQL response.

    The frontend response stores fixture odds under:
      fixture.defaultMarketsInfo.defaultMarket
      fixture.defaultMarketsInfo.threewayDefaultMarkets[]

    Human-readable selection names are stored separately under
    display.selectionGroups, so this parser joins them by selection ID.
    """
    root = payload.get("data", payload) if isinstance(payload, dict) else payload
    markets = []
    seen = set()

    def walk(node, ctx, path="$", depth=0):
        if depth > 30:
            return
        if isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, ctx, f"{path}[{i}]", depth + 1)
            return
        if not isinstance(node, dict):
            return

        c = dict(ctx)

        category = node.get("category")
        if isinstance(category, dict):
            sport = category.get("sports") or category.get("sport")
            if sport:
                c["sport"] = str(sport)
            category_name = category.get("name")
            if category_name:
                c["category"] = str(category_name)

        # Competition containers carry the tournament name and fixtures.
        if isinstance(node.get("fixtures"), dict):
            tournament = node.get("name") or node.get("title")
            if tournament:
                c["tournament"] = str(tournament)

        default_info = node.get("defaultMarketsInfo")
        if isinstance(default_info, dict):
            home, away = _shuffle_competitor_names(node)
            event = str(node.get("name") or "").strip()
            if not event and home and away:
                event = f"{home} - {away}"

            fixture_ctx = dict(c)
            fixture_ctx.update({
                "eventId": str(node.get("id") or node.get("slug") or ""),
                "event": event,
                "team1": home,
                "team2": away,
                "startTime": node.get("startTime"),
                "status": node.get("status"),
            })

            default_market = default_info.get("defaultMarket")
            _parse_shuffle_market_bundle(
                default_market,
                fixture_ctx,
                f"{path}.defaultMarketsInfo.defaultMarket",
                markets,
                seen,
            )

            for i, bundle in enumerate(default_info.get("threewayDefaultMarkets", []) or []):
                _parse_shuffle_market_bundle(
                    bundle,
                    fixture_ctx,
                    f"{path}.defaultMarketsInfo.threewayDefaultMarkets[{i}]",
                    markets,
                    seen,
                )

        for key, value in node.items():
            # defaultMarketsInfo was already parsed explicitly; walking into it
            # would only rediscover duplicate selection fragments.
            if key == "defaultMarketsInfo":
                continue
            walk(value, c, f"{path}.{key}", depth + 1)

    walk(root, {})
    return markets

def fetch_shuffle_markets():
    """Fetch Shuffle soccer markets.

    UPCOMING mode (default) reproduces the public Shuffle Upcoming tab:
    GetSportsFixtures + UPCOMING_ONLY, following nextCursor automatically.
    CUSTOM mode keeps compatibility with SHUFFLE_GRAPHQL_BODY.
    """
    use_upcoming = SHUFFLE_SCAN_MODE != "CUSTOM"

    if not use_upcoming and not SHUFFLE_GRAPHQL_BODY:
        return {
            "ok": False,
            "stage": "configuration",
            "markets": [],
            "errors": ["SHUFFLE_GRAPHQL_BODY is not configured for CUSTOM mode."],
            "scanMode": SHUFFLE_SCAN_MODE,
        }

    if not use_upcoming:
        try:
            custom_body = json.loads(SHUFFLE_GRAPHQL_BODY)
        except Exception as exc:
            return {
                "ok": False,
                "stage": "configuration",
                "markets": [],
                "errors": [f"Invalid SHUFFLE_GRAPHQL_BODY JSON: {exc}"],
                "scanMode": SHUFFLE_SCAN_MODE,
            }

    markets = []
    errors = []
    pages_loaded = 0
    fixtures_discovered = 0
    cursor = None

    try:
        max_pages = SHUFFLE_MAX_PAGES if use_upcoming else 1

        for page_index in range(max_pages):
            body = _shuffle_upcoming_body(cursor) if use_upcoming else custom_body

            response = requests.post(
                SHUFFLE_GRAPHQL_URL,
                headers=_shuffle_headers(),
                json=body,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            pages_loaded += 1

            gql_errors = payload.get("errors", []) if isinstance(payload, dict) else []
            if gql_errors:
                errors.extend([
                    {"page": page_index + 1, "error": item}
                    for item in gql_errors
                ])

            page_markets = parse_shuffle_markets(payload)
            markets.extend(page_markets)

            if not use_upcoming:
                break

            sports_fixtures = (
                payload.get("data", {}).get("sportsFixtures")
                if isinstance(payload, dict)
                and isinstance(payload.get("data"), dict)
                else None
            )

            if not isinstance(sports_fixtures, dict):
                break

            nodes = sports_fixtures.get("nodes")
            if isinstance(nodes, list):
                fixtures_discovered += len(nodes)

            next_cursor = sports_fixtures.get("nextCursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        # Cursor pages should be disjoint, but dedupe defensively.
        unique = []
        seen = set()
        for market in markets:
            key = (
                market.get("eventId") or _norm_text(market.get("event")),
                _norm_text(market.get("market")),
                tuple(
                    sorted(
                        (
                            _norm_text(outcome.get("selection")),
                            float(outcome.get("odds") or 0),
                        )
                        for outcome in market.get("outcomes", [])
                    )
                ),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(market)

        return {
            "ok": len(errors) == 0,
            "stage": "complete",
            "scanMode": "UPCOMING" if use_upcoming else "CUSTOM",
            "pagesLoaded": pages_loaded,
            "fixturesDiscovered": fixtures_discovered,
            "markets": unique,
            "errors": errors,
        }

    except Exception as exc:
        return {
            "ok": False,
            "stage": "request",
            "scanMode": "UPCOMING" if use_upcoming else "CUSTOM",
            "pagesLoaded": pages_loaded,
            "fixturesDiscovered": fixtures_discovered,
            "markets": markets,
            "errors": [f"{type(exc).__name__}: {exc}"],
        }


def _norm_text(value):
    import re
    import unicodedata
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().strip()
    replacements = {
        " utd ": " united ",
        " st ": " saint ",
    }
    value = " " + value + " "
    for old, replacement in replacements.items():
        value = value.replace(old, replacement)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _normalize_team(value):
    text = _norm_text(value)
    if not text:
        return ""
    tokens = text.split()
    # Club suffixes vary a lot between books and add no identity information.
    suffixes = {"fc", "cf", "afc", "sc", "fk", "sk", "ac", "club"}
    while len(tokens) > 1 and tokens[-1] in suffixes:
        tokens.pop()
    return " ".join(tokens)


def _event_teams(market):
    t1 = _normalize_team(market.get("team1"))
    t2 = _normalize_team(market.get("team2"))
    if t1 and t2:
        return t1, t2

    event = str(market.get("event") or "")
    import re
    parts = re.split(r"\s+(?:vs?\.?|[-–—])\s+", event, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return _normalize_team(parts[0]), _normalize_team(parts[1])
    return _normalize_team(event), ""


def _canonical_market_type(market):
    raw = _norm_text(market.get("marketType") or market.get("market"))
    compact = raw.replace(" ", "")
    if compact in {"p1xp2", "1x2"}:
        return "1x2"
    if any(x in raw for x in ("match result", "full time result", "fulltime result", "threeway", "three way")):
        return "1x2"

    # Some feeds use a generic market name but the three outcomes themselves
    # clearly identify the standard football 1-X-2 market.
    outcomes = market.get("outcomes", []) or []
    labels = {_norm_text(o.get("type") or o.get("selection") or o.get("name")) for o in outcomes if isinstance(o, dict)}
    excluded = ("half", "corner", "card", "booking", "period", "set", "map", "quarter")
    if len(outcomes) == 3 and not any(x in raw for x in excluded) and ("draw" in labels or "x" in labels):
        return "1x2"
    return raw


def _team_similarity(a, b):
    from difflib import SequenceMatcher
    a = _normalize_team(a)
    b = _normalize_team(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / max(1, len(ta | tb))
    sequence = SequenceMatcher(None, a, b).ratio()
    # Exact containment is useful for names such as "arsenal" vs "arsenal london".
    containment = 1.0 if (a in b or b in a) and min(len(a), len(b)) >= 5 else 0.0
    return max(sequence, jaccard, containment * 0.93)


def _parse_start_time(value):
    if value is None or value == "":
        return None
    try:
        # Stake can return Unix epoch milliseconds while Shuffle uses ISO-8601.
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            ts = float(value)
            if ts > 10_000_000_000:  # milliseconds
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)

        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _event_context_flags(market):
    # Prevent similarly named but fundamentally different fixtures from matching
    # (for example Stake SRL/virtual football against a real Shuffle match).
    import re
    raw = _norm_text(" ".join(str(market.get(k) or "") for k in (
        "event", "sport", "tournament", "category", "league"
    )))
    padded = f" {raw} "
    return {
        "simulated": any(x in padded for x in (
            " srl ", " simulated ", " simulated reality ", " virtual "
        )),
        "esports": any(x in padded for x in (
            " esports ", " esport ", " esoccer ", " e soccer "
        )),
        "women": any(x in padded for x in (
            " women ", " womens ", " ladies ", " female "
        )),
        "reserve": any(x in padded for x in (
            " reserves ", " reserve team ", " b team "
        )),
        "youth": bool(re.search(r"\b(?:u|under)\s?(?:16|17|18|19|20|21|23)\b|\byouth\b", raw)),
    }


def _event_match(market_a, market_b):
    flags_a = _event_context_flags(market_a)
    flags_b = _event_context_flags(market_b)
    for key in flags_a:
        if flags_a[key] != flags_b[key]:
            return 0.0, False

    a1, a2 = _event_teams(market_a)
    b1, b2 = _event_teams(market_b)
    if not all((a1, a2, b1, b2)):
        return 0.0, False

    direct_parts = (_team_similarity(a1, b1), _team_similarity(a2, b2))
    swapped_parts = (_team_similarity(a1, b2), _team_similarity(a2, b1))
    direct = sum(direct_parts) / 2
    swapped = sum(swapped_parts) / 2
    score, is_swapped, parts = (direct, False, direct_parts) if direct >= swapped else (swapped, True, swapped_parts)

    # Both team names must independently be credible; this prevents one common
    # club name from causing an unrelated fixture match.
    if min(parts) < 0.72:
        return 0.0, is_swapped

    ta = _parse_start_time(market_a.get("startTime"))
    tb = _parse_start_time(market_b.get("startTime"))
    if ta and tb:
        hours = abs((ta - tb).total_seconds()) / 3600.0
        if hours > 6:
            return 0.0, is_swapped
        if hours <= 0.5:
            score = min(1.0, score + 0.04)
        elif hours > 2:
            score -= 0.04

    return score, is_swapped


def _canonical_outcome(market, outcome):
    raw = _normalize_team(outcome.get("type") or outcome.get("selection") or outcome.get("name"))
    compact = raw.replace(" ", "")
    if compact in {"p1", "1", "home", "team1", "w1"}:
        return "1"
    if compact in {"x", "draw", "tie"}:
        return "X"
    if compact in {"p2", "2", "away", "team2", "w2"}:
        return "2"

    t1, t2 = _event_teams(market)
    if raw and _team_similarity(raw, t1) >= 0.88:
        return "1"
    if raw and _team_similarity(raw, t2) >= 0.88:
        return "2"
    return None


def _dedupe_1x2(markets, bookmaker):
    result = []
    seen = set()
    for market in markets:
        if market.get("bookmaker") != bookmaker or _canonical_market_type(market) != "1x2":
            continue
        teams = _event_teams(market)
        if not teams[0] or not teams[1]:
            continue
        # Prefer eventId where present, otherwise the normalized teams/start time.
        event_key = str(market.get("eventId") or "").strip()
        if not event_key:
            start = _parse_start_time(market.get("startTime"))
            start_key = start.strftime("%Y-%m-%dT%H:%M") if start else ""
            event_key = f"{teams[0]}|{teams[1]}|{start_key}"
        if event_key in seen:
            continue
        seen.add(event_key)
        result.append(market)
    return result


def find_cross_book_matches(markets, threshold=0.82):
    stake = _dedupe_1x2(markets, "Stake")
    shuffle = _dedupe_1x2(markets, "Shuffle")
    candidates = []

    for sm in stake:
        best = None
        for hm in shuffle:
            score, swapped = _event_match(sm, hm)
            if score < threshold:
                continue
            if best is None or score > best[0]:
                best = (score, swapped, hm)
        if best:
            score, swapped, hm = best
            candidates.append({
                "stake": sm,
                "shuffle": hm,
                "score": score,
                "swapped": swapped,
            })
    return candidates


def _best_outcomes_for_pair(stake_market, shuffle_market, swapped=False):
    best = {}
    for market in (stake_market, shuffle_market):
        for outcome in market.get("outcomes", []) or []:
            label = _canonical_outcome(market, outcome)
            if label not in {"1", "X", "2"}:
                continue
            if market.get("bookmaker") == "Shuffle" and swapped:
                if label == "1":
                    label = "2"
                elif label == "2":
                    label = "1"
            try:
                odds = float(outcome.get("odds"))
            except (TypeError, ValueError):
                continue
            if odds <= 1:
                continue
            candidate = {
                "bookmaker": market.get("bookmaker"),
                "selection": outcome.get("selection") or outcome.get("name") or label,
                "canonicalSelection": label,
                "odds": odds,
            }
            if label not in best or odds > best[label]["odds"]:
                best[label] = candidate
    return best


def find_1x2_arbitrage(markets, bankroll, min_arb_percent):
    results = []
    seen_events = set()
    for pair in find_cross_book_matches(markets):
        stake_market = pair["stake"]
        shuffle_market = pair["shuffle"]
        best = _best_outcomes_for_pair(stake_market, shuffle_market, pair["swapped"])
        if set(best) != {"1", "X", "2"}:
            continue

        arb_sum = sum(1.0 / best[x]["odds"] for x in ("1", "X", "2"))
        arb_percent = (1.0 - arb_sum) * 100.0
        if arb_sum >= 1.0 or arb_percent < min_arb_percent:
            continue

        teams = _event_teams(stake_market)
        event_key = teams
        if event_key in seen_events:
            continue
        seen_events.add(event_key)

        guaranteed_return = bankroll / arb_sum
        legs = []
        for label in ("1", "X", "2"):
            item = dict(best[label])
            item["stake"] = round(bankroll * (1.0 / item["odds"]) / arb_sum, 2)
            legs.append(item)

        results.append({
            "event": stake_market.get("event") or f"{teams[0]} - {teams[1]}",
            "shuffleEvent": shuffle_market.get("event"),
            "market": "1X2",
            "matchConfidence": round(pair["score"], 4),
            "arbPercent": round(arb_percent, 4),
            "impliedProbabilitySum": round(arb_sum, 6),
            "bankroll": bankroll,
            "guaranteedReturn": round(guaranteed_return, 2),
            "guaranteedProfit": round(guaranteed_return - bankroll, 2),
            "legs": legs,
        })

    return sorted(results, key=lambda x: x["arbPercent"], reverse=True)



# ---------------------------------------------------------------------------
# v9 two-way focused arbitrage engine
# ---------------------------------------------------------------------------
# v9 intentionally ignores soccer 1X2. It asks Shuffle for the public
# sportsbook's Total and Handicap default-market views directly by using the
# same prioritizedMarketTypeId values exposed by Shuffle's GetSports query.
# Only half-lines (x.5) are considered guaranteed two-way markets here.

V9_SUPPORTED_FAMILIES = ("total", "spread")
SHUFFLE_TOTAL_MARKET_TYPE_ID = os.getenv(
    "SHUFFLE_TOTAL_MARKET_TYPE_ID", "18_BETRADAR"
).strip()
SHUFFLE_HANDICAP_MARKET_TYPE_ID = os.getenv(
    "SHUFFLE_HANDICAP_MARKET_TYPE_ID", "16_BETRADAR"
).strip()

# Keep the previous loader available for CUSTOM mode compatibility.
_fetch_shuffle_markets_pre_v9 = fetch_shuffle_markets


def _raw_market_text(market):
    return " ".join(str(market.get(k) or "") for k in (
        "market", "marketType", "displayType", "groupKey"
    )).strip()


def _contains_any(text, terms):
    return any(term in text for term in terms)


def _is_main_match_market_v9(market):
    """Reject props/period markets that are not full-match two-way markets."""
    text = " " + _norm_text(_raw_market_text(market)) + " "
    excluded = (
        " first half ", " 1st half ", " second half ", " 2nd half ",
        " half time ", " halftime ", " quarter ", " period ", " inning ",
        " set ", " map ", " corner ", " corners ", " card ", " cards ",
        " booking ", " bookings ", " player ", " scorer ", " shots ",
        " throw in ", " offsides ", " penalty shootout ", " to qualify ",
        " outright ", " early payout ", " 2up ", " 2 up ",
        " team total ", " home total ", " away total ",
    )
    return not _contains_any(text, excluded)


def _coerce_float(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _numbers_from_text(value):
    import re
    text = str(value or "").replace("−", "-").replace("–", "-").replace(",", ".")
    return [float(x) for x in re.findall(r"(?<![A-Za-z0-9])([+-]?\d+(?:\.\d+)?)(?![A-Za-z0-9])", text)]


def _line_is_half(value):
    """Only x.5 lines are used: no push and no quarter-line split settlement."""
    if value is None:
        return False
    doubled = abs(float(value) * 2.0)
    return abs(doubled - round(doubled)) < 1e-8 and int(round(doubled)) % 2 == 1


def _line_key(value):
    value = float(value)
    if abs(value) < 1e-10:
        value = 0.0
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text if text != "-0" else "0"


def _selection_raw(outcome):
    return str(outcome.get("selection") or outcome.get("name") or outcome.get("type") or "").strip()


def _outcome_line(outcome):
    for key in ("lineValue", "line", "handicap", "points", "base"):
        value = _coerce_float(outcome.get(key))
        if value is not None:
            return value
    return None


def _selection_team_side_v9(market, outcome):
    """Return 1/2 for a team selection, tolerating handicap text around names."""
    import re
    raw = _selection_raw(outcome)
    norm = _norm_text(raw)
    compact = norm.replace(" ", "")
    if compact in {"p1", "1", "home", "team1", "w1", "hometeam"}:
        return "1"
    if compact in {"p2", "2", "away", "team2", "w2", "awayteam"}:
        return "2"

    cleaned = raw.replace("−", "-").replace("–", "-")
    cleaned = re.sub(r"[\(\[]?\s*[+-]?\d+(?:[\.,]\d+)?\s*[\)\]]?", " ", cleaned)
    cleaned = re.sub(r"\b(?:home|away|team\s*[12]|p[12]|w[12])\b", " ", cleaned, flags=re.I)
    cleaned_norm = _normalize_team(cleaned)

    t1, t2 = _event_teams(market)
    if cleaned_norm and t1 and _team_similarity(cleaned_norm, t1) >= 0.82:
        return "1"
    if cleaned_norm and t2 and _team_similarity(cleaned_norm, t2) >= 0.82:
        return "2"

    old = _canonical_outcome(market, outcome)
    return old if old in {"1", "2"} else None


def _over_under_side_v9(outcome):
    import re
    raw = _selection_raw(outcome).lower()
    norm = _norm_text(raw)
    if re.search(r"\bover\b", raw) or norm in {"o", "over"}:
        return "over"
    if re.search(r"\bunder\b", raw) or norm in {"u", "under"}:
        return "under"
    return None


def _extract_total_line_v9(market):
    # Provider-level structured line is the safest source.
    structured = _coerce_float(market.get("lineValue"))
    if structured is not None:
        return abs(structured)

    # Some Stake payloads attach the same total line to both outcomes.
    outcome_lines = []
    for outcome in market.get("outcomes", []) or []:
        if _over_under_side_v9(outcome):
            line = _outcome_line(outcome)
            if line is not None:
                outcome_lines.append(abs(line))
    if outcome_lines and max(outcome_lines) - min(outcome_lines) < 1e-8:
        return outcome_lines[0]

    # Finally parse labels such as "Over 2.5" / "Under 2.5".
    lines = []
    for outcome in market.get("outcomes", []) or []:
        if _over_under_side_v9(outcome):
            nums = _numbers_from_text(_selection_raw(outcome))
            if nums:
                lines.append(abs(nums[-1]))
    if lines and max(lines) - min(lines) < 1e-8:
        return lines[0]

    nums = _numbers_from_text(_raw_market_text(market))
    return abs(nums[-1]) if nums else None


def _extract_home_spread_line_v9(market):
    # Prefer per-selection structured/text lines because they establish which
    # sign belongs to team1/home.
    home_lines = []
    away_lines = []
    for outcome in market.get("outcomes", []) or []:
        side = _selection_team_side_v9(market, outcome)
        line = _outcome_line(outcome)
        if line is None:
            nums = _numbers_from_text(_selection_raw(outcome))
            if nums:
                line = nums[-1]
        if line is None:
            continue
        if side == "1":
            home_lines.append(line)
        elif side == "2":
            away_lines.append(line)

    if home_lines:
        return home_lines[0]
    if away_lines:
        return -away_lines[0]

    structured = _coerce_float(market.get("lineValue"))
    if structured is not None:
        return structured

    nums = _numbers_from_text(_raw_market_text(market))
    return nums[-1] if nums else None


def _market_descriptor_v9(market, swapped=False):
    """Return a canonical Total/Handicap descriptor, or None."""
    if not _is_main_match_market_v9(market):
        return None

    outcomes = [x for x in (market.get("outcomes", []) or []) if isinstance(x, dict)]
    if len(outcomes) != 2:
        return None

    raw = " " + _norm_text(_raw_market_text(market)) + " "
    requested = str(market.get("requestedMarketFamily") or "").strip().lower()

    ou_sides = {_over_under_side_v9(x) for x in outcomes}
    ou_sides.discard(None)
    looks_total = requested == "total" or ou_sides == {"over", "under"} or _contains_any(
        raw, (" total ", " over under ", " over and under ", " number of goals ")
    )
    if looks_total and ou_sides == {"over", "under"}:
        line = _extract_total_line_v9(market)
        if not _line_is_half(line):
            return None
        key = f"total:{_line_key(line)}"
        return {
            "family": "total",
            "key": key,
            "line": line,
            "label": f"Total {_line_key(line)}",
        }

    looks_spread = requested == "spread" or _contains_any(
        raw, (" handicap ", " spread ", " asian handicap ", " match handicap ")
    )
    if looks_spread:
        sides = {_selection_team_side_v9(market, x) for x in outcomes}
        sides.discard(None)
        if sides != {"1", "2"}:
            return None
        home_line = _extract_home_spread_line_v9(market)
        if home_line is None:
            return None
        if swapped:
            home_line = -home_line
        if not _line_is_half(home_line):
            return None
        key = f"spread:{_line_key(home_line)}"
        return {
            "family": "spread",
            "key": key,
            "line": home_line,
            "label": f"Handicap {_line_key(home_line)}",
        }

    return None


def _canonical_selection_v9(market, outcome, descriptor, swapped=False):
    family = descriptor.get("family")
    if family == "total":
        return _over_under_side_v9(outcome)
    if family == "spread":
        label = _selection_team_side_v9(market, outcome)
        if swapped and label in {"1", "2"}:
            label = "2" if label == "1" else "1"
        return label
    return None


def _event_group_key_v9(market):
    event_id = str(market.get("eventId") or "").strip()
    if event_id:
        return event_id
    t1, t2 = _event_teams(market)
    start = _parse_start_time(market.get("startTime"))
    minute = start.strftime("%Y-%m-%dT%H:%M") if start else ""
    return f"{t1}|{t2}|{minute}"


def _book_event_groups_v9(markets, bookmaker):
    groups = {}
    for market in markets:
        if market.get("bookmaker") != bookmaker:
            continue
        t1, t2 = _event_teams(market)
        if not t1 or not t2:
            continue
        key = _event_group_key_v9(market)
        groups.setdefault(key, []).append(market)
    return list(groups.values())


def _representative_market_v9(group):
    # Prefer a recognized two-way market, then any market with event metadata.
    ranked = []
    for market in group:
        desc = _market_descriptor_v9(market)
        priority = {"total": 0, "spread": 1}.get(desc.get("family") if desc else None, 9)
        ranked.append((priority, market))
    ranked.sort(key=lambda x: x[0])
    return ranked[0][1] if ranked else None


def find_event_matches_v9(markets, threshold=0.82):
    stake_groups = _book_event_groups_v9(markets, "Stake")
    shuffle_groups = _book_event_groups_v9(markets, "Shuffle")
    shuffle_reps = [(g, _representative_market_v9(g)) for g in shuffle_groups]
    candidates = []

    for sg in stake_groups:
        sm = _representative_market_v9(sg)
        if not sm:
            continue
        best = None
        for hg, hm in shuffle_reps:
            if not hm:
                continue
            score, swapped = _event_match(sm, hm)
            if score < threshold:
                continue
            if best is None or score > best[0]:
                best = (score, swapped, hg, hm)
        if best:
            score, swapped, hg, hm = best
            candidates.append({
                "stake": sm,
                "shuffle": hm,
                "stakeMarkets": sg,
                "shuffleMarkets": hg,
                "score": score,
                "swapped": swapped,
            })
    return candidates


def _descriptor_map_v9(group, swapped=False):
    result = {}
    for market in group:
        desc = _market_descriptor_v9(market, swapped=swapped)
        if not desc:
            continue
        result.setdefault(desc["key"], {"descriptor": desc, "markets": []})["markets"].append(market)
    return result


def _expected_labels_v9(family):
    if family == "total":
        return ("over", "under")
    if family == "spread":
        return ("1", "2")
    return ()


def _best_prices_v9(stake_markets, shuffle_markets, descriptor, swapped):
    best = {}
    for bookmaker, group, is_swapped in (
        ("Stake", stake_markets, False),
        ("Shuffle", shuffle_markets, swapped),
    ):
        for market in group:
            desc = _market_descriptor_v9(market, swapped=is_swapped)
            if not desc or desc.get("key") != descriptor.get("key"):
                continue
            for outcome in market.get("outcomes", []) or []:
                label = _canonical_selection_v9(market, outcome, desc, swapped=is_swapped)
                if label not in _expected_labels_v9(descriptor.get("family")):
                    continue
                try:
                    odds = float(outcome.get("odds"))
                except (TypeError, ValueError):
                    continue
                if odds <= 1.0:
                    continue
                candidate = {
                    "bookmaker": bookmaker,
                    "selection": _selection_raw(outcome) or label,
                    "canonicalSelection": label,
                    "odds": odds,
                    "sourceMarket": market.get("market"),
                    "line": descriptor.get("line"),
                }
                if label not in best or odds > best[label]["odds"]:
                    best[label] = candidate
    return best


def _best_book_prices_v10(markets, descriptor, bookmaker, swapped=False):
    """Best price for each side from one bookmaker on one exact canonical line."""
    best = {}
    expected = _expected_labels_v9(descriptor.get("family"))
    for market in markets:
        desc = _market_descriptor_v9(market, swapped=swapped)
        if not desc or desc.get("key") != descriptor.get("key"):
            continue
        for outcome in market.get("outcomes", []) or []:
            label = _canonical_selection_v9(market, outcome, desc, swapped=swapped)
            if label not in expected:
                continue
            try:
                odds = float(outcome.get("odds"))
            except (TypeError, ValueError):
                continue
            if odds <= 1.0:
                continue
            candidate = {
                "bookmaker": bookmaker,
                "selection": _selection_raw(outcome) or label,
                "canonicalSelection": label,
                "odds": odds,
                "sourceMarket": market.get("market"),
                "line": descriptor.get("line"),
            }
            if label not in best or odds > best[label]["odds"]:
                best[label] = candidate
    return best


def _cross_book_candidate_v10(pair, key, descriptor, bankroll):
    """Return the best genuine Stake-vs-Shuffle two-leg combination for one line.

    v9 selected the best price for each outcome globally and then rejected a line if
    both best prices came from the same book. v10 explicitly checks both legal
    cross-book orientations, so a valid near-miss or arbitrage is never hidden by
    a slightly better same-book price.
    """
    labels = _expected_labels_v9(descriptor.get("family"))
    if len(labels) != 2:
        return None

    stake_map = _descriptor_map_v9(pair["stakeMarkets"], swapped=False)
    shuffle_map = _descriptor_map_v9(pair["shuffleMarkets"], swapped=pair["swapped"])
    if key not in stake_map or key not in shuffle_map:
        return None

    stake_prices = _best_book_prices_v10(
        stake_map[key]["markets"], descriptor, "Stake", swapped=False
    )
    shuffle_prices = _best_book_prices_v10(
        shuffle_map[key]["markets"], descriptor, "Shuffle", swapped=pair["swapped"]
    )

    a, b = labels
    combos = []
    if a in stake_prices and b in shuffle_prices:
        combos.append((stake_prices[a], shuffle_prices[b]))
    if a in shuffle_prices and b in stake_prices:
        combos.append((shuffle_prices[a], stake_prices[b]))
    if not combos:
        return None

    best_combo = None
    for leg1, leg2 in combos:
        implied_sum = (1.0 / leg1["odds"]) + (1.0 / leg2["odds"])
        arb_percent = (1.0 - implied_sum) * 100.0
        if best_combo is None or arb_percent > best_combo[0]:
            best_combo = (arb_percent, implied_sum, leg1, leg2)

    arb_percent, implied_sum, leg1, leg2 = best_combo
    balanced_return = bankroll / implied_sum
    legs = []
    for item in (leg1, leg2):
        leg = dict(item)
        leg["stake"] = round(bankroll * (1.0 / leg["odds"]) / implied_sum, 2)
        legs.append(leg)

    teams = _event_teams(pair["stake"])
    return {
        "event": pair["stake"].get("event") or f"{teams[0]} - {teams[1]}",
        "shuffleEvent": pair["shuffle"].get("event"),
        "market": descriptor.get("label"),
        "marketFamily": descriptor.get("family"),
        "marketKey": key,
        "matchConfidence": round(pair["score"], 4),
        "arbPercent": round(arb_percent, 4),
        "distanceToArbitragePercent": round(max(0.0, -arb_percent), 4),
        "impliedProbabilitySum": round(implied_sum, 6),
        "bankroll": bankroll,
        "balancedReturn": round(balanced_return, 2),
        "balancedProfit": round(balanced_return - bankroll, 2),
        "isArbitrage": implied_sum < 1.0,
        "legs": legs,
    }


def find_twoway_candidates_v10(markets, bankroll):
    """All matched two-way lines ranked from best arb to closest near-miss."""
    results = []
    seen = set()
    for pair in find_event_matches_v9(markets):
        stake_map = _descriptor_map_v9(pair["stakeMarkets"], swapped=False)
        shuffle_map = _descriptor_map_v9(pair["shuffleMarkets"], swapped=pair["swapped"])
        common_keys = sorted(set(stake_map) & set(shuffle_map))
        for key in common_keys:
            descriptor = stake_map[key]["descriptor"]
            teams = _event_teams(pair["stake"])
            unique_key = (teams, key)
            if unique_key in seen:
                continue
            candidate = _cross_book_candidate_v10(pair, key, descriptor, bankroll)
            if not candidate:
                continue
            seen.add(unique_key)
            results.append(candidate)
    return sorted(results, key=lambda x: x["arbPercent"], reverse=True)


def find_twoway_arbitrage_v10(markets, bankroll, min_arb_percent):
    return [
        x for x in find_twoway_candidates_v10(markets, bankroll)
        if x["isArbitrage"] and x["arbPercent"] >= min_arb_percent
    ]


def find_twoway_arbitrage_v9(markets, bankroll, min_arb_percent):
    results = []
    seen = set()
    for pair in find_event_matches_v9(markets):
        stake_map = _descriptor_map_v9(pair["stakeMarkets"], swapped=False)
        shuffle_map = _descriptor_map_v9(pair["shuffleMarkets"], swapped=pair["swapped"])
        common_keys = sorted(set(stake_map) & set(shuffle_map))

        for key in common_keys:
            descriptor = stake_map[key]["descriptor"]
            labels = _expected_labels_v9(descriptor.get("family"))
            if not labels:
                continue
            best = _best_prices_v9(
                stake_map[key]["markets"],
                shuffle_map[key]["markets"],
                descriptor,
                pair["swapped"],
            )
            if any(label not in best for label in labels):
                continue
            if len({best[label]["bookmaker"] for label in labels}) < 2:
                continue

            arb_sum = sum(1.0 / best[label]["odds"] for label in labels)
            arb_percent = (1.0 - arb_sum) * 100.0
            if arb_sum >= 1.0 or arb_percent < min_arb_percent:
                continue

            teams = _event_teams(pair["stake"])
            unique_key = (teams, key)
            if unique_key in seen:
                continue
            seen.add(unique_key)

            guaranteed_return = bankroll / arb_sum
            legs = []
            for label in labels:
                item = dict(best[label])
                item["stake"] = round(bankroll * (1.0 / item["odds"]) / arb_sum, 2)
                legs.append(item)

            results.append({
                "event": pair["stake"].get("event") or f"{teams[0]} - {teams[1]}",
                "shuffleEvent": pair["shuffle"].get("event"),
                "market": descriptor.get("label"),
                "marketFamily": descriptor.get("family"),
                "marketKey": key,
                "matchConfidence": round(pair["score"], 4),
                "arbPercent": round(arb_percent, 4),
                "impliedProbabilitySum": round(arb_sum, 6),
                "bankroll": bankroll,
                "guaranteedReturn": round(guaranteed_return, 2),
                "guaranteedProfit": round(guaranteed_return - bankroll, 2),
                "legs": legs,
            })

    return sorted(results, key=lambda x: x["arbPercent"], reverse=True)


def _market_family_counts_v9(markets, bookmaker):
    counts = {name: 0 for name in V9_SUPPORTED_FAMILIES}
    keys = set()
    for market in markets:
        if market.get("bookmaker") != bookmaker:
            continue
        desc = _market_descriptor_v9(market)
        if not desc:
            continue
        signature = (_event_group_key_v9(market), desc["key"])
        if signature in keys:
            continue
        keys.add(signature)
        counts[desc["family"]] += 1
    counts["totalComparable2WayMarkets"] = sum(counts.values())
    return counts


def _sample_comparable_v9(markets, bookmaker, limit=12):
    samples = []
    seen = set()
    for market in markets:
        if market.get("bookmaker") != bookmaker:
            continue
        desc = _market_descriptor_v9(market)
        if not desc:
            continue
        key = (_event_group_key_v9(market), desc["key"])
        if key in seen:
            continue
        seen.add(key)
        samples.append({
            "event": market.get("event"),
            "startTime": market.get("startTime"),
            "market": market.get("market"),
            "marketType": market.get("marketType"),
            "family": desc.get("family"),
            "key": desc.get("key"),
            "line": desc.get("line"),
            "outcomes": market.get("outcomes"),
            "requestedMarketFamily": market.get("requestedMarketFamily"),
            "prioritizedMarketTypeId": market.get("prioritizedMarketTypeId"),
        })
        if len(samples) >= limit:
            break
    return samples


def _shuffle_scan_prioritized_v9(family, market_type_id):
    markets = []
    errors = []
    pages_loaded = 0
    fixtures_discovered = 0
    cursor = None

    try:
        for page_index in range(SHUFFLE_MAX_PAGES):
            body = _shuffle_upcoming_body(cursor)
            body["variables"]["prioritizedMarketTypeId"] = market_type_id

            response = requests.post(
                SHUFFLE_GRAPHQL_URL,
                headers=_shuffle_headers(),
                json=body,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            pages_loaded += 1

            gql_errors = payload.get("errors", []) if isinstance(payload, dict) else []
            if gql_errors:
                errors.extend([
                    {"family": family, "page": page_index + 1, "error": item}
                    for item in gql_errors
                ])

            page_markets = parse_shuffle_markets(payload)
            for market in page_markets:
                market["requestedMarketFamily"] = family
                market["prioritizedMarketTypeId"] = market_type_id
            markets.extend(page_markets)

            sports_fixtures = (
                payload.get("data", {}).get("sportsFixtures")
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict)
                else None
            )
            if not isinstance(sports_fixtures, dict):
                break

            nodes = sports_fixtures.get("nodes")
            if isinstance(nodes, list):
                fixtures_discovered += len(nodes)

            next_cursor = sports_fixtures.get("nextCursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

    except Exception as exc:
        errors.append({
            "family": family,
            "error": f"{type(exc).__name__}: {exc}",
        })

    return {
        "family": family,
        "marketTypeId": market_type_id,
        "pagesLoaded": pages_loaded,
        "fixturesDiscovered": fixtures_discovered,
        "markets": markets,
        "errors": errors,
    }


def fetch_shuffle_markets():
    """v9: fetch Shuffle Total and Handicap views in parallel.

    Shuffle's sports UI exposes default market IDs 18_BETRADAR (Total) and
    16_BETRADAR (Handicap). Passing one as prioritizedMarketTypeId makes that
    two-way market the fixture's default market where available.
    """
    if SHUFFLE_SCAN_MODE == "CUSTOM":
        return _fetch_shuffle_markets_pre_v9()

    specs = [
        ("total", SHUFFLE_TOTAL_MARKET_TYPE_ID),
        ("spread", SHUFFLE_HANDICAP_MARKET_TYPE_ID),
    ]

    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        futures = [pool.submit(_shuffle_scan_prioritized_v9, family, market_id) for family, market_id in specs]
        scans = [future.result() for future in futures]

    all_markets = []
    all_errors = []
    for scan in scans:
        all_markets.extend(scan.get("markets", []))
        all_errors.extend(scan.get("errors", []))

    # The same fixture appears in both prioritized scans. Keep each distinct
    # market/line once while preserving Total and Handicap side by side.
    unique = []
    seen = set()
    for market in all_markets:
        desc = _market_descriptor_v9(market)
        descriptor_key = desc.get("key") if desc else (
            _norm_text(market.get("market")),
            _coerce_float(market.get("lineValue")),
        )
        key = (
            market.get("eventId") or _norm_text(market.get("event")),
            descriptor_key,
            tuple(sorted(
                (_norm_text(outcome.get("selection")), float(outcome.get("odds") or 0))
                for outcome in market.get("outcomes", [])
            )),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(market)

    scan_info = [
        {
            "family": scan.get("family"),
            "marketTypeId": scan.get("marketTypeId"),
            "pagesLoaded": scan.get("pagesLoaded", 0),
            "fixturesDiscovered": scan.get("fixturesDiscovered", 0),
            "marketsLoaded": len(scan.get("markets", [])),
            "errors": scan.get("errors", []),
        }
        for scan in scans
    ]

    return {
        "ok": len(all_errors) == 0,
        "stage": "complete",
        "scanMode": "UPCOMING_TWO_WAY",
        "pagesLoaded": sum(scan.get("pagesLoaded", 0) for scan in scans),
        "fixturesDiscovered": max([scan.get("fixturesDiscovered", 0) for scan in scans] or [0]),
        "markets": unique,
        "errors": all_errors,
        "prioritizedScans": scan_info,
    }


@app.get("/api/shuffle-debug")
def shuffle_debug():
    data = fetch_shuffle_markets()
    markets = data.get("markets", [])
    return jsonify({
        "ok": data.get("ok", False),
        "version": SCANNER_VERSION,
        "stage": data.get("stage"),
        "graphqlUrl": SHUFFLE_GRAPHQL_URL,
        "scanMode": data.get("scanMode"),
        "totalMarketTypeId": SHUFFLE_TOTAL_MARKET_TYPE_ID,
        "handicapMarketTypeId": SHUFFLE_HANDICAP_MARKET_TYPE_ID,
        "prioritizedScans": data.get("prioritizedScans", []),
        "marketsLoaded": len(markets),
        "comparable2Way": _market_family_counts_v9(markets, "Shuffle"),
        "sampleComparable2Way": _sample_comparable_v9(markets, "Shuffle", 10),
        "errors": data.get("errors", []),
    })


@app.get("/api/shuffle-markets")
def shuffle_markets():
    data = fetch_shuffle_markets()
    return jsonify({
        "ok": data.get("ok", False),
        "version": SCANNER_VERSION,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "marketsLoaded": len(data.get("markets", [])),
        "quotesLoaded": sum(len(m.get("outcomes", [])) for m in data.get("markets", [])),
        "markets": data.get("markets", []),
        "prioritizedScans": data.get("prioritizedScans", []),
        "errors": data.get("errors", []),
    })


def fetch_both_books_parallel():
    """Fetch Stake plus Shuffle's two prioritized feeds concurrently."""
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        stake_future = pool.submit(fetch_stake_markets)
        shuffle_future = pool.submit(fetch_shuffle_markets)
        stake_data = stake_future.result()
        shuffle_data = shuffle_future.result()
    return stake_data, shuffle_data, round(time.monotonic() - started, 3)


@app.get("/api/twoway-debug")
def twoway_debug():
    try:
        stake_data, shuffle_data, fetch_seconds = fetch_both_books_parallel()
        combined = stake_data.get("markets", []) + shuffle_data.get("markets", [])
        matches = find_event_matches_v9(combined)

        common_event_count = 0
        common_market_pairs = 0
        family_common = {"total": 0, "spread": 0}
        match_samples = []

        for pair in matches:
            stake_map = _descriptor_map_v9(pair["stakeMarkets"])
            shuffle_map = _descriptor_map_v9(pair["shuffleMarkets"], swapped=pair["swapped"])
            common = sorted(set(stake_map) & set(shuffle_map))
            if common:
                common_event_count += 1
                common_market_pairs += len(common)
                for key in common:
                    family = stake_map[key]["descriptor"].get("family")
                    if family in family_common:
                        family_common[family] += 1
            if len(match_samples) < 20:
                match_samples.append({
                    "stakeEvent": pair["stake"].get("event"),
                    "shuffleEvent": pair["shuffle"].get("event"),
                    "score": round(pair["score"], 4),
                    "swapped": pair["swapped"],
                    "common2WayMarkets": common,
                })

        return jsonify({
            "ok": True,
            "version": SCANNER_VERSION,
            "fetchSeconds": fetch_seconds,
            "supportedFamilies": list(V9_SUPPORTED_FAMILIES),
            "stakeMarketsLoaded": len(stake_data.get("markets", [])),
            "shuffleMarketsLoaded": len(shuffle_data.get("markets", [])),
            "stakeComparable": _market_family_counts_v9(combined, "Stake"),
            "shuffleComparable": _market_family_counts_v9(combined, "Shuffle"),
            "matchedEvents": len(matches),
            "matchedEventsWithCommon2Way": common_event_count,
            "common2WayMarketPairs": common_market_pairs,
            "commonByFamily": family_common,
            "shufflePrioritizedScans": shuffle_data.get("prioritizedScans", []),
            "stakeSamples": _sample_comparable_v9(combined, "Stake", 10),
            "shuffleSamples": _sample_comparable_v9(combined, "Shuffle", 10),
            "sampleMatches": match_samples,
            "errors": {
                "stake": stake_data.get("errors", []),
                "shuffle": shuffle_data.get("errors", []),
            },
        })
    except Exception as exc:
        return jsonify({
            "ok": False,
            "version": SCANNER_VERSION,
            "stage": "twoway-debug",
            "errorType": type(exc).__name__,
            "error": str(exc),
        }), 500


@app.get("/api/match-debug")
def match_debug():
    # Keep the familiar URL but make its v9 output specifically about two-way markets.
    return twoway_debug()


@app.get("/api/closest")
def closest_opportunities():
    bankroll = float(request.args.get("bankroll", os.getenv("BANKROLL", "1000")))
    try:
        limit = int(request.args.get("limit", "20"))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 100))

    try:
        stake_data, shuffle_data, fetch_seconds = fetch_both_books_parallel()
        combined = stake_data.get("markets", []) + shuffle_data.get("markets", [])
        candidates = find_twoway_candidates_v10(combined, bankroll)
    except Exception as exc:
        return jsonify({
            "ok": False,
            "version": SCANNER_VERSION,
            "stage": "closest",
            "errorType": type(exc).__name__,
            "error": str(exc),
            "closest": [],
        }), 500

    family_counts = {name: 0 for name in V9_SUPPORTED_FAMILIES}
    for item in candidates:
        family = item.get("marketFamily")
        if family in family_counts:
            family_counts[family] += 1

    return jsonify({
        "ok": True,
        "version": SCANNER_VERSION,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "fetchSeconds": fetch_seconds,
        "bankroll": bankroll,
        "supportedFamilies": list(V9_SUPPORTED_FAMILIES),
        "candidateCounts": family_counts,
        "totalCandidates": len(candidates),
        "positiveArbitrages": sum(1 for x in candidates if x.get("isArbitrage")),
        "note": "Sorted best first. Positive arbPercent is a mathematical arbitrage; negative arbPercent is a near-miss. Always re-check live odds and settlement rules before betting.",
        "closest": candidates[:limit],
        "errors": {
            "stake": stake_data.get("errors", []),
            "shuffle": shuffle_data.get("errors", []),
        },
    })


@app.get("/api/opportunities")
def opportunities():
    bankroll = float(request.args.get("bankroll", os.getenv("BANKROLL", "1000")))
    minimum = float(request.args.get("min_arb_percent", os.getenv("MIN_ARB_PERCENT", "0.5")))

    try:
        stake_data, shuffle_data, fetch_seconds = fetch_both_books_parallel()
        combined = stake_data.get("markets", []) + shuffle_data.get("markets", [])
        candidates = find_twoway_candidates_v10(combined, bankroll)
        arbs = [x for x in candidates if x["isArbitrage"] and x["arbPercent"] >= minimum]
    except Exception as exc:
        return jsonify({
            "ok": False,
            "version": SCANNER_VERSION,
            "stage": "opportunities",
            "errorType": type(exc).__name__,
            "error": str(exc),
            "opportunities": [],
        }), 500

    family_counts = {name: 0 for name in V9_SUPPORTED_FAMILIES}
    for arb in arbs:
        family = arb.get("marketFamily")
        if family in family_counts:
            family_counts[family] += 1

    return jsonify({
        "ok": True,
        "version": SCANNER_VERSION,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "fetchSeconds": fetch_seconds,
        "bankroll": bankroll,
        "minArbPercent": minimum,
        "supportedFamilies": list(V9_SUPPORTED_FAMILIES),
        "ignoredMarket": "Soccer 1X2 is intentionally excluded in v10.",
        "safetyRule": "Only full-match Total/Handicap half-lines (x.5) are treated as guaranteed two-way markets; whole and quarter lines are excluded.",
        "stakeMarketsLoaded": len(stake_data.get("markets", [])),
        "shuffleMarketsLoaded": len(shuffle_data.get("markets", [])),
        "stakeComparable": _market_family_counts_v9(combined, "Stake"),
        "shuffleComparable": _market_family_counts_v9(combined, "Shuffle"),
        "opportunityCounts": family_counts,
        "opportunities": arbs,
        "closest": candidates[:10],
        "closestNote": "arbPercent above 0 is arbitrage; negative values show how far the best cross-book pair is from arbitrage.",
        "errors": {
            "stake": stake_data.get("errors", []),
            "shuffle": shuffle_data.get("errors", []),
        },
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
