# SpinAndBet + Stake Arbitrage Scanner — Render Ready

This package is designed so you can deploy the scanner in the cloud without installing Python or Node.js on your PC.

## What you get

- One Render web service
- Browser dashboard served by the same Flask app
- `/api/opportunities` endpoint
- Demo mode
- Configurable Stake feed
- Configurable SpinAndBet feed
- Arbitrage calculation and stake allocation
- No automatic bet placement

## Easiest deployment route

1. Create a GitHub account if you do not already have one.
2. Create a new repository.
3. Upload every file from this folder to that repository.
4. Sign in to Render.
5. Choose **New +** → **Blueprint**.
6. Connect the GitHub repository.
7. Render will detect `render.yaml`.
8. Deploy.

After deployment, Render gives you a public HTTPS address such as:

`https://your-scanner.onrender.com`

Open that address on your phone or PC.

## Demo mode

The project starts with:

`USE_DEMO_DATA=true`

So the scanner works immediately after deployment.

## Switch to live mode

In Render:

1. Open your Web Service.
2. Go to **Environment**.
3. Change:

`USE_DEMO_DATA=false`

4. Add the permitted odds-feed configuration:

`STAKE_FEED_URL`

`STAKE_AUTH_HEADER`

`STAKE_AUTH_VALUE`

`SPINANDBET_FEED_URL`

`SPINANDBET_AUTH_HEADER`

`SPINANDBET_AUTH_VALUE`

5. Redeploy.

Do not put sportsbook API keys in the browser or GitHub repository.

## Feed format

The generic adapter accepts a raw JSON list or an object containing `odds`, `data`, or `results`.

Each row should normalize to:

```json
{
  "event_id": "abc123",
  "event": "Team A vs Team B",
  "sport": "football",
  "league": "Example League",
  "market": "moneyline",
  "selection": "Team A",
  "odds": 2.20
}
```

If a provider returns a different schema, modify `providers.py`.

## Important

This package is scanner-only. It does not place bets or bypass login, CAPTCHA, anti-bot protections, or other access controls. Confirm sportsbook terms, market rules, settlement rules, limits, and applicable laws before using live data.
