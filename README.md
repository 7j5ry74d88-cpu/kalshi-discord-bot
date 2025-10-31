# Kalshi Bot (Discord) — Liquidity Seers

A simple Discord bot that searches Kalshi markets, lets you set price alerts, and shows active movers.

## Commands
- `/find <keyword>` — find open markets
- `/watch <TICKER> [threshold]` — watch a market and (optionally) alert when YES ≤ threshold
- `/unwatch <TICKER>` — remove from watchlist
- `/list` — list watches
- `/movers` — top open markets by rough activity

## Quick Deploy (Railway)
1. Create a new project from this repo.
2. Add environment variable: `DISCORD_TOKEN` (from Discord Developer Portal → your bot → Reset Token).
3. Ensure start command is `python bot.py` (or keep `Procfile` set to `worker: python bot.py`).
4. Deploy. When running, invite the bot to your server via OAuth2 URL (scopes: bot, applications.commands; perms: Send Messages, View Channels, Use Slash Commands).

## Notes
- Uses Kalshi **public** market data endpoints (no Kalshi auth required).
- Alerts post to the **first text channel** in the guild by default; you can adjust in `alert_loop()`.
