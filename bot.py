# bot.py
import os, json, asyncio
from pathlib import Path
import discord
from discord import app_commands
import httpx

# -------------- Basic Config --------------
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
WATCH_FILE = DATA_DIR / "watches.json"

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN environment variable. Set it in your host (Railway).")

# Public market-data base (no auth)
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# -------------- Storage --------------
def load_watches():
    try:
        if WATCH_FILE.exists():
            return json.loads(WATCH_FILE.read_text())
    except Exception:
        pass
    return {}

def save_watches(w):
    try:
        WATCH_FILE.write_text(json.dumps(w, indent=2))
    except Exception as e:
        print("Failed to save watches:", e)

watches = load_watches()  # {guild_id: {ticker: {"threshold": float|None}}}

# -------------- Discord Client --------------
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# -------------- Kalshi Helpers --------------
async def kalshi_get(path, params=None):
    async with httpx.AsyncClient(timeout=10.0) as s:
        r = await s.get(f"{KALSHI_BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()

async def fetch_markets_open(limit=200):
    results = []
    cursor = None
    while True:
        params = {"status": "open", "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = await kalshi_get("/markets", params)
        results.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor or len(results) >= 1000:  # safety cap
            break
    return results

async def fetch_market(ticker):
    return await kalshi_get(f"/markets/{ticker}")

async def fetch_orderbook(ticker):
    return await kalshi_get(f"/markets/{ticker}/orderbook")

def best_price_from_book(book_side):
    if not book_side:
        return None
    return max(level.get("price", 0) for level in book_side) / 100.0

def yes_from_no(no_price):
    if no_price is None:
        return None
    return round(1 - no_price, 2)

async def market_snapshot(ticker):
    # Returns (yes_price_estimate, no_best_bid) in dollars
    try:
        ob = await fetch_orderbook(ticker)
        yes_bid = best_price_from_book(ob.get("yes", []))
        no_bid  = best_price_from_book(ob.get("no", []))
        yes_price = yes_bid if yes_bid is not None else (yes_from_no(no_bid) if no_bid is not None else None)
        return yes_price, no_bid
    except Exception as e:
        # Fallback: try market endpoint (may not contain last price reliably)
        try:
            m = await fetch_market(ticker)
            last_yes = m.get("market", {}).get("last_price", None)
            return (last_yes/100.0 if isinstance(last_yes, int) else None), None
        except Exception:
            return None, None

# -------------- Slash Commands --------------
@tree.command(name="find", description="Find Kalshi markets by keyword")
@app_commands.describe(query="Keyword to search in market titles")
async def find_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    mkts = await fetch_markets_open()
    q = query.lower().strip()
    hits = []
    for m in mkts:
        title = m.get("title", "")
        if q in title.lower():
            ticker = m.get("ticker")
            yes, _ = await market_snapshot(ticker)
            hits.append((title, ticker, yes))
            if len(hits) >= 10:
                break
    if not hits:
        await interaction.followup.send(f"No open markets matched `{query}`.")
        return
    lines = []
    for title, ticker, yes in hits:
        yes_str = f"{yes:.2f}" if yes is not None else "—"
        lines.append(f"**{title}**\n`{ticker}` • YES≈${yes_str}")
    await interaction.followup.send("\n\n".join(lines))

@tree.command(name="watch", description="Watch a market; optional YES threshold to alert")
@app_commands.describe(ticker="Kalshi market ticker (e.g., KXGOVSHUTLENGTH-26JAN01)",
                       threshold="Alert when YES <= threshold (e.g., 0.35)")
async def watch_cmd(interaction: discord.Interaction, ticker: str, threshold: float | None = None):
    gid = str(interaction.guild_id)
    w = watches.get(gid, {})
    w[ticker.upper()] = {"threshold": threshold}
    watches[gid] = w
    save_watches(watches)
    msg = f"Watching `{ticker.upper()}`"
    if threshold is not None:
        msg += f" (alert when YES ≤ ${threshold:.2f})"
    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="unwatch", description="Remove a watched market")
@app_commands.describe(ticker="Kalshi market ticker")
async def unwatch_cmd(interaction: discord.Interaction, ticker: str):
    gid = str(interaction.guild_id)
    w = watches.get(gid, {})
    removed = w.pop(ticker.upper(), None)
    watches[gid] = w
    save_watches(watches)
    if removed:
        await interaction.response.send_message(f"Removed `{ticker.upper()}` from watchlist.", ephemeral=True)
    else:
        await interaction.response.send_message(f"`{ticker.upper()}` was not being watched.", ephemeral=True)

@tree.command(name="list", description="List watched markets for this server")
async def list_cmd(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    w = watches.get(gid, {})
    if not w:
        await interaction.response.send_message("No watches set.", ephemeral=True)
        return
    lines = []
    for t, cfg in w.items():
        thr = cfg.get("threshold")
        thr_text = f" (YES ≤ ${thr:.2f})" if thr is not None else ""
        lines.append(f"`{t}`{thr_text}")
    await interaction.response.send_message("Watches:\n" + "\n".join(lines), ephemeral=True)

@tree.command(name="movers", description="Show top open markets by rough activity")
async def movers_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    mkts = await fetch_markets_open()
    scored = []
    for m in mkts:
        ticker = m.get("ticker")
        title = m.get("title", "")
        vol = m.get("volume", 0) or 0
        yes, no_bid = await market_snapshot(ticker)
        rng = 0.0
        if yes is not None and no_bid is not None:
            rng = abs(yes - (1 - no_bid))
        score = (vol or 0) + rng * 100
        scored.append((score, title, ticker, yes, vol))
    top = sorted(scored, key=lambda x: x[0], reverse=True)[:10]
    if not top:
        await interaction.followup.send("No open markets found.")
        return
    out = []
    for _, title, ticker, yes, vol in top:
        yes_str = f"{yes:.2f}" if yes is not None else "—"
        out.append(f"**{title}**\n`{ticker}` • YES≈${yes_str} • vol={vol}")
    await interaction.followup.send("\n\n".join(out))

# -------------- Background Alert Loop --------------
async def alert_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            for gid, w in list(watches.items()):
                guild = client.get_guild(int(gid)) if gid.isdigit() else None
                if not guild or not guild.text_channels:
                    continue
                # Post to the first text channel by default; you can change this later.
                target_channel = guild.text_channels[0]
                for ticker, cfg in list(w.items()):
                    thr = cfg.get("threshold")
                    if thr is None:
                        continue
                    yes, _ = await market_snapshot(ticker)
                    if yes is not None and yes <= thr:
                        await target_channel.send(f"🔔 Alert: `{ticker}` YES is ${yes:.2f} (≤ ${thr:.2f})")
                        # One-shot: clear threshold so it doesn't spam
                        w[ticker]["threshold"] = None
                        save_watches(watches)
            await asyncio.sleep(60)  # check every 60s
        except Exception as e:
            # brief backoff and keep going
            await asyncio.sleep(5)

@client.event
async def on_ready():
    try:
        await tree.sync()  # global sync; can take a couple minutes to appear
        print(f"✅ Logged in as {client.user}")
    except Exception as e:
        print("Slash sync failed:", e)

async def main():
    client.loop.create_task(alert_loop())
    await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    # Optional: uvloop if available
    try:
        import uvloop
        uvloop.install()
    except Exception:
        pass
    asyncio.run(main())
