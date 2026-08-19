import os
import requests
from arbitrage import Quote


STAKE_BASE_URL = os.getenv("STAKE_BASE_URL", "https://odds-data.stake.com").rstrip("/")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))
STAKE_FIXTURE_LIMIT = int(os.getenv("STAKE_FIXTURE_LIMIT", "30"))

# Keep the auth header configurable. Stake's docs name the auth scheme "apiKey".
# Default header name is "apiKey"; if your issued credentials specify another
# header name, change STAKE_API_KEY_HEADER in Render without changing code.
STAKE_API_KEY_HEADER = os.getenv("STAKE_API_KEY_HEADER", "apiKey").strip()
STAKE_API_KEY = os.getenv("STAKE_API_KEY", "").strip()

# Comma-separated Stake sport slugs. Start small while testing.
# Examples commonly include football, basketball, tennis, etc.
STAKE_SPORTS = [
    x.strip() for x in os.getenv("STAKE_SPORTS", "football").split(",")
    if x.strip()
]


class StakeProvider:
    bookmaker = "Stake"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if STAKE_API_KEY:
            self.session.headers[STAKE_API_KEY_HEADER] = STAKE_API_KEY

    def _get(self, path):
        response = self.session.get(
            f"{STAKE_BASE_URL}{path}",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def fetch(self):
        if not STAKE_API_KEY:
            return []

        quotes = []
        seen_fixture_slugs = set()

        for sport_slug in STAKE_SPORTS:
            # Official endpoint:
            # GET /sport/{sport}/fixture
            payload = self._get(f"/sport/{sport_slug}/fixture")
            fixtures = payload.get("fixture", []) if isinstance(payload, dict) else []

            for fixture in fixtures:
                slug = str(fixture.get("slug", "")).strip()
                if not slug or slug in seen_fixture_slugs:
                    continue
                seen_fixture_slugs.add(slug)

                try:
                    fixture_payload = self._get(f"/fixtures/{slug}")
                    quotes.extend(self._parse_fixture(sport_slug, fixture_payload))
                except requests.RequestException:
                    # One bad/suspended fixture should not stop the entire scan.
                    continue

                if len(seen_fixture_slugs) >= STAKE_FIXTURE_LIMIT:
                    return quotes

        return quotes

    def _parse_fixture(self, sport_slug, payload):
        fixture = payload.get("fixture", payload) if isinstance(payload, dict) else {}
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
            or fixture.get("category")
            or ""
        )

        quotes = []

        # Official fixture response contains groups -> markets -> outcomes.
        for group in fixture.get("groups", []) or []:
            for market in group.get("markets", []) or []:
                if str(market.get("status", "active")).lower() != "active":
                    continue

                market_name = str(
                    market.get("name")
                    or group.get("name")
                    or "market"
                )

                outcomes = [
                    o for o in (market.get("outcomes", []) or [])
                    if o.get("active", True)
                    and o.get("odds") is not None
                ]

                # Phase 1 scanner only handles 2-way markets.
                if len(outcomes) != 2:
                    continue

                for outcome in outcomes:
                    try:
                        odds = float(outcome["odds"])
                    except (TypeError, ValueError, KeyError):
                        continue

                    if odds <= 1:
                        continue

                    quotes.append(Quote(
                        bookmaker=self.bookmaker,
                        event_id=fixture_id,
                        event=event_name,
                        sport=sport_slug,
                        league=league,
                        market=market_name,
                        selection=str(outcome.get("name", "")),
                        odds=odds,
                    ))

        return quotes


class SpinAndBetJsonProvider:
    """
    Read-only adapter for a permitted SpinAndBet JSON feed/API.

    This deliberately does not scrape logged-in pages, bypass CAPTCHA,
    or defeat anti-bot/access controls.
    """

    bookmaker = "SpinAndBet"

    def __init__(self):
        self.url = os.getenv("SPINANDBET_FEED_URL", "").strip()
        self.auth_header = os.getenv("SPINANDBET_AUTH_HEADER", "").strip()
        self.auth_value = os.getenv("SPINANDBET_AUTH_VALUE", "").strip()

    def fetch(self):
        if not self.url:
            return []

        headers = {"Accept": "application/json"}
        if self.auth_header and self.auth_value:
            headers[self.auth_header] = self.auth_value

        response = requests.get(
            self.url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()

        if isinstance(payload, dict):
            rows = payload.get(
                "odds",
                payload.get("data", payload.get("results", []))
            )
        else:
            rows = payload

        quotes = []
        for x in rows:
            try:
                odds = float(x["odds"])
                if odds <= 1:
                    continue

                quotes.append(Quote(
                    bookmaker=self.bookmaker,
                    event_id=str(x["event_id"]),
                    event=str(x.get("event", "")),
                    sport=str(x.get("sport", "")),
                    league=str(x.get("league", "")),
                    market=str(x["market"]),
                    selection=str(x["selection"]),
                    odds=odds,
                ))
            except (KeyError, TypeError, ValueError):
                continue

        return quotes


def demo_quotes():
    return [
        Quote("SpinAndBet", "demo-1", "Team A vs Team B", "football",
              "Demo League", "moneyline", "Team A", 2.35),
        Quote("SpinAndBet", "demo-1", "Team A vs Team B", "football",
              "Demo League", "moneyline", "Team B", 1.82),
        Quote("Stake", "demo-1", "Team A vs Team B", "football",
              "Demo League", "moneyline", "Team A", 1.91),
        Quote("Stake", "demo-1", "Team A vs Team B", "football",
              "Demo League", "moneyline", "Team B", 2.05),
    ]


def get_all_quotes():
    if os.getenv("USE_DEMO_DATA", "true").lower() in ("1", "true", "yes"):
        return demo_quotes()

    quotes = []
    quotes.extend(StakeProvider().fetch())
    quotes.extend(SpinAndBetJsonProvider().fetch())
    return quotes
