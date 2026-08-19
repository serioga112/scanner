import os
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, send_from_directory

app = Flask(__name__, static_folder=None)

STAKE_BASE_URL = os.getenv("STAKE_BASE_URL", "https://odds-data.stake.com").rstrip("/")
STAKE_API_KEY = os.getenv("STAKE_API_KEY", "").strip()
STAKE_API_KEY_HEADER = os.getenv("STAKE_API_KEY_HEADER", "apiKey").strip()
STAKE_SPORT = os.getenv("STAKE_SPORT", "football").strip()
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


def extract_fixture_list(payload):
    if isinstance(payload, dict):
        for key in ("fixture", "fixtures", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    if isinstance(payload, list):
        return payload
    return []


def first_fixture_slug():
    payload = stake_get(f"/sport/{STAKE_SPORT}/fixture")
    fixtures = extract_fixture_list(payload)

    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue

        slug = str(fixture.get("slug") or fixture.get("id") or "").strip()
        if slug:
            return slug, fixture

    return None, None


def search_paths(obj, path="$", matches=None, limit=500):
    if matches is None:
        matches = []

    if len(matches) >= limit:
        return matches

    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}"

            key_l = str(key).lower()

            if key_l in {
                "markets", "market", "outcomes", "outcome",
                "odds", "price", "prices", "selection", "selections"
            }:
                preview = None

                if isinstance(value, list):
                    preview = {
                        "type": "list",
                        "length": len(value),
                        "firstItemKeys": list(value[0].keys()) if value and isinstance(value[0], dict) else None,
                    }
                elif isinstance(value, dict):
                    preview = {
                        "type": "dict",
                        "keys": list(value.keys())[:30],
                    }
                else:
                    preview = {
                        "type": type(value).__name__,
                        "value": value,
                    }

                matches.append({
                    "path": child_path,
                    "key": key,
                    "preview": preview,
                })

                if len(matches) >= limit:
                    return matches

            search_paths(value, child_path, matches, limit)

    elif isinstance(obj, list):
        for i, value in enumerate(obj[:100]):
            child_path = f"{path}[{i}]"
            search_paths(value, child_path, matches, limit)

    return matches


def redact_sensitive(obj):
    sensitive_keys = {
        "apikey", "api_key", "authorization", "token", "secret",
        "password", "cookie", "set-cookie", "session"
    }

    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in sensitive_keys:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_sensitive(v)
        return out

    if isinstance(obj, list):
        return [redact_sensitive(x) for x in obj]

    return obj


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


@app.get("/api/stake-paths")
def stake_paths():
    if not STAKE_API_KEY:
        return jsonify({
            "ok": False,
            "error": "STAKE_API_KEY is not configured",
        }), 400

    try:
        slug, fixture_meta = first_fixture_slug()

        if not slug:
            return jsonify({
                "ok": False,
                "error": "No fixture found",
            }), 404

        raw = stake_get(f"/fixtures/{slug}")
        matches = search_paths(raw)

        return jsonify({
            "ok": True,
            "fixtureSlug": slug,
            "fixtureMeta": redact_sensitive(fixture_meta),
            "matchCount": len(matches),
            "paths": matches,
        })

    except Exception as exc:
        return jsonify({
            "ok": False,
            "errorType": type(exc).__name__,
            "error": str(exc),
        }), 502
