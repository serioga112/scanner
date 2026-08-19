from dataclasses import dataclass
from typing import List, Dict


@dataclass
class Quote:
    bookmaker: str
    event_id: str
    event: str
    sport: str
    league: str
    market: str
    selection: str
    odds: float


def find_arbs(quotes: List[Quote], bankroll: float, min_arb_percent: float):
    groups: Dict[str, List[Quote]] = {}

    for q in quotes:
        if q.odds <= 1:
            continue
        key = f"{q.event_id.strip().lower()}|{q.market.strip().lower()}"
        groups.setdefault(key, []).append(q)

    results = []

    for key, group in groups.items():
        best = {}
        for q in group:
            s = q.selection.strip().lower()
            if s not in best or q.odds > best[s].odds:
                best[s] = q

        if len(best) != 2:
            continue

        a, b = list(best.values())
        arb_sum = 1 / a.odds + 1 / b.odds

        if arb_sum >= 1:
            continue

        arb_percent = (1 - arb_sum) * 100
        if arb_percent < min_arb_percent:
            continue

        stake_a = bankroll * (1 / a.odds) / arb_sum
        stake_b = bankroll * (1 / b.odds) / arb_sum
        guaranteed_return = stake_a * a.odds

        results.append({
            "id": key,
            "event": a.event or b.event,
            "sport": a.sport or b.sport,
            "league": a.league or b.league,
            "market": a.market,
            "selectionA": a.selection,
            "bookmakerA": a.bookmaker,
            "oddsA": a.odds,
            "selectionB": b.selection,
            "bookmakerB": b.bookmaker,
            "oddsB": b.odds,
            "arbPercent": round(arb_percent, 4),
            "stakeA": round(stake_a, 2),
            "stakeB": round(stake_b, 2),
            "guaranteedReturn": round(guaranteed_return, 2),
            "profit": round(guaranteed_return - bankroll, 2),
        })

    return sorted(results, key=lambda x: x["arbPercent"], reverse=True)
