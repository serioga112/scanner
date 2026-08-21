# Shuffle + Stake Arbitrage Scanner

Scanner-only Flask project for comparing Stake and Shuffle soccer 1X2 odds.

## Deploy on Render
1. Upload this folder to GitHub.
2. Create a Render Blueprint from the repository.
3. In Render Environment, set `STAKE_API_KEY`.
4. Capture Shuffle's public sportsbook GraphQL POST request body (the JSON containing `operationName`, `variables`, and/or `query`) and set it as `SHUFFLE_GRAPHQL_BODY`.
5. Do not copy account cookies, passwords, 2FA codes, or Authorization tokens into the project.
6. Redeploy.

## Test
- `/api/stake-debug`
- `/api/shuffle-debug`
- `/api/opportunities?bankroll=1000&min_arb_percent=0.5`

The Shuffle parser is intentionally schema-tolerant, but the exact public GraphQL request body still has to be captured from the current Shuffle sports frontend before live Shuffle markets can load.
