import os
import requests
from arbitrage import Quote


class JsonFeedProvider:
    def __init__(self, bookmaker, url_env, auth_header_env, auth_value_env):
        self.bookmaker = bookmaker
        self.url = os.getenv(url_env, "").strip()
        self.auth_header = os.getenv(auth_header_env, "").strip()
        self.auth_value = os.getenv(auth_value_env, "").strip()
        self.timeout = float(os.getenv("REQUEST_TIMEOUT", "10"))

    def fetch(self):
        if not self.url:
            return []

        headers = {"Accept": "application/json"}
        if self.auth_header and self.auth_value:
            headers[self.auth_header] = self.auth_value

        response = requests.get(self.url, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()

        if isinstance(payload, dict):
            rows = payload.get("odds", payload.get("data", payload.get("results", [])))
        else:
            rows = payload

        return [self._map_row(x) for x in rows]

    def _map_row(self, x):
        return Quote(
            bookmaker=self.bookmaker,
            event_id=str(x["event_id"]),
            event=str(x.get("event", "")),
            sport=str(x.get("sport", "")),
            league=str(x.get("league", "")),
            market=str(x["market"]),
            selection=str(x["selection"]),
            odds=float(x["odds"]),
        )


def demo_quotes():
    return [
        Quote("SpinAndBet", "demo-1", "Team A vs Team B", "football", "Demo League", "moneyline", "Team A", 2.35),
        Quote("SpinAndBet", "demo-1", "Team A vs Team B", "football", "Demo League", "moneyline", "Team B", 1.82),
        Quote("Stake", "demo-1", "Team A vs Team B", "football", "Demo League", "moneyline", "Team A", 1.91),
        Quote("Stake", "demo-1", "Team A vs Team B", "football", "Demo League", "moneyline", "Team B", 2.05),
    ]


def get_all_quotes():
    if os.getenv("USE_DEMO_DATA", "true").lower() in ("1", "true", "yes"):
        return demo_quotes()

    stake = JsonFeedProvider(
        "Stake",
        "STAKE_FEED_URL",
        "STAKE_AUTH_HEADER",
        "STAKE_AUTH_VALUE",
    )

    spin = JsonFeedProvider(
        "SpinAndBet",
        "SPINANDBET_FEED_URL",
        "SPINANDBET_AUTH_HEADER",
        "SPINANDBET_AUTH_VALUE",
    )

    return stake.fetch() + spin.fetch()
