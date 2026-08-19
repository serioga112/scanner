import os
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template, request

from arbitrage import find_arbs
from providers import get_all_quotes

app = Flask(__name__)


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "time": datetime.now(timezone.utc).isoformat()
    })


@app.get("/api/opportunities")
def opportunities():
    bankroll = float(request.args.get("bankroll", os.getenv("BANKROLL", "1000")))
    minimum = float(request.args.get("min_arb_percent", os.getenv("MIN_ARB_PERCENT", "0.5")))

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
            "opportunities": []
        }), 502
