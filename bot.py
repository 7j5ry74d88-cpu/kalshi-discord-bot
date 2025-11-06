# bot.py
import os, json, asyncio, re, time
from pathlib import Path
import discord
from discord import app_commands
import httpx
from aiohttp import web  # health server

# -------------- Basic Config --------------
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
WATCH_FILE = DATA_DIR / "watches.json"
HISTORY_FILE = DATA_DIR / "histories.json"  # for /vol deltas

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN environment variable.")

# Optional proxy (e.g., Cloudflare Worker) to bypass WAF on Replit
PROXY_BASE = os.environ.get("PROXY_BASE")  # e.g., https://your-worker.workers.dev/api

# If you want commands to appear in ANY server the bot joins, set GUILD_IDS = []
GUILD_IDS = []  # empty -> global commands
GUILDS = [discord.Object(id=i) for i in GUILD_IDS]

# Try proxy first (if provided), then official APIs
KALSHI_BASES = ([PROXY_BASE] if PROXY_BASE else []) + [
    "https://api.kalshi.com/trade-api/v2",
    "https://api.elections.kalshi.com/trade-api/v2",
]

HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Origin": "https://kalshi.com",
    "Referer": "https://kalshi.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Connection": "keep-alive",
}

# -------------- Storage --------------
def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default

def save_json(path: Path, obj):
    try:
        path.write_text(json.dumps(obj, indent=2))
    except Exception as e:
        print(f"Failed to save {path.name}:", e)

watches = load_json(WATCH_FILE, {})
histories = load_json(HISTORY_FILE, {})  # {ticker: [[ts, yes], ...]}

def record_price(ticker: str, yes_price: float | None):
    """Keep a rolling tape of (ts, yes) for /vol deltas. Trim to ~72h."""
    if yes_price is None:
        return
    now = int(time.time())
    arr = histories.get(ticker, [])
    arr.append([now, float(yes_price)])
    # keep only last 72h
    cutoff = now - 72*3600
    arr = [row for row in arr if row[0] >= cutoff]
    histories[ticker] = arr
    save_json(HISTORY_FILE, histories)

# -------------- Discord Client --------------
intents = discord.Intents.default()

class MyClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.bg_task = None

    async def setup_hook(self):
        self.bg_task = asyncio.create_task(alert_loop())

client = MyClient(intents=intents)
tree = client.tree

# -------------- Helpers --------------
def to_cents_str(val: float | None) -> str:
    """Format a 0–1 price as cents like '65¢', with proper handling for low values."""
    if val is None:
        return "—"
    try:
        cents = float(val) * 100
        if cents == 0:
            return "0¢"
        elif cents < 1:
            return f"{cents:.2f}¢"  # Show decimals for very low prices
        else:
            return f"{round(cents):.0f}¢"
    except Exception:
        return "—"

def chunk_lines(lines, max_chars=1800, sep="\n\n"):
    chunk, length = [], 0
    for line in lines:
        add = (sep if chunk else "") + line
        if length + len(add) > max_chars:
            if chunk:
                yield sep.join(chunk)
            chunk, length = [line], len(line)
        else:
            chunk.append(line)
            length += len(add)
    if chunk:
        yield sep.join(chunk)

KALSHI_URL_TICKER_RE = re.compile(r'(KX[A-Z0-9][A-Z0-9-._]+)', re.I)

def extract_ticker(text: str) -> str | None:
    """Find a Kalshi ticker either directly or inside a URL."""
    if not text:
        return None
    m = KALSHI_URL_TICKER_RE.search(text.upper())
    return m.group(1).strip() if m else None

# Simple topic hints to make /find friendlier for non-experts
TOPIC_HINTS = {
    "sports": ["nfl", "nba", "mlb", "nhl", "premier", "soccer", "tennis", "atp", "wta", "ncaa", "ufc"],
    "politics": ["election", "senate", "house", "president", "approval", "primary", "poll"],
    "economy": ["cpi", "inflation", "jobs", "unemployment", "gdp", "fed", "rate", "interest"],
    "weather": ["temperature", "rain", "snow", "storm", "degree", "daily high", "daily low"],
}

# -------------- Kalshi API --------------
async def kalshi_get(path, params=None):
    last_err = None
    for base in KALSHI_BASES:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0),
                headers=HEADERS,
                follow_redirects=True,
                http2=False,
            ) as s:
                r = await s.get(f"{base}{path}", params=params)
                ctype = r.headers.get("Content-Type", "")
                if "application/json" not in ctype:
                    raise RuntimeError(f"Non-JSON from {base}: {r.status_code} {ctype}")
                r.raise_for_status()
                return r.json()
        except Exception as e:
            last_err = e
            continue
    raise last_err

async def fetch_markets_open(limit=200):
    seen, results = set(), []

    async def pull(status="open"):
        cursor = None
        while True:
            params = {"limit": limit, "status": status}
            if cursor:
                params["cursor"] = cursor
            data = await kalshi_get("/markets", params)
            mkts = data.get("markets", []) or []
            for m in mkts:
                t = m.get("ticker")
                if t and t not in seen:
                    seen.add(t)
                    results.append(m)
            cursor = data.get("cursor")
            if not cursor or len(results) >= 1200:
                break

    # Only fetch OPEN markets, skip closed/expired ones
    await pull("open")
    
    # If we need more results, also include "active" markets
    if len(results) < 50:
        await pull("active")

    return results

async def fetch_market(ticker):
    try:
        data = await kalshi_get(f"/markets/{ticker}")
        return data
    except Exception as e:
        # Check if it's a 404 error
        if "404" in str(e) or "Not Found" in str(e):
            raise Exception(f"Market not found - may be expired or invalid: {ticker}")
        else:
            raise e

async def fetch_orderbook(ticker):
    return await kalshi_get(f"/markets/{ticker}/orderbook")

# IMPROVED: Market snapshot with actual current prices and status checking
async def market_snapshot(ticker: str):
    """
    Returns (yes_price, no_price, last_price, volume, status) with proper current prices
    """
    yes_price = None
    no_price = None
    last_price = None
    volume = 0
    status = "unknown"

    try:
        # Get the actual market data which contains the current price
        market_data = await fetch_market(ticker)
        market_info = market_data.get("market", {})
        
        status = market_info.get("status", "unknown")
        
        # If market is closed, return None prices
        if status in ["closed", "expired", "settled"]:
            return None, None, None, volume, status
        
        # The current YES price is in 'yes_ask' (to buy YES) or 'last_price'
        yes_ask = market_info.get("yes_ask")
        yes_bid = market_info.get("yes_bid") 
        last_price = market_info.get("last_price")
        volume = market_info.get("volume", 0)
        
        # Priority: yes_ask (current price to buy YES) -> yes_bid -> last_price
        if yes_ask is not None:
            yes_price = yes_ask / 100.0
        elif yes_bid is not None:
            yes_price = yes_bid / 100.0
        elif last_price is not None:
            yes_price = last_price / 100.0
            
        # NO price is simply 1 - YES price
        if yes_price is not None:
            no_price = round(1 - yes_price, 4)
            
    except Exception as e:
        print(f"Error fetching market data for {ticker}: {e}")
        # Fallback to orderbook if market endpoint fails
        try:
            ob = await fetch_orderbook(ticker)
            yside = ob.get("yes", []) or []
            if yside:
                best_ask = min(level.get("price", 100) for level in yside)  # Lowest ask price
                yes_price = best_ask / 100.0
                no_price = 1 - yes_price
        except Exception as ob_error:
            print(f"Orderbook fallback also failed: {ob_error}")

    return yes_price, no_price, last_price, volume, status

def market_volume_from_payload(market_payload: dict) -> int:
    try:
        return int((market_payload.get("market", {}) or {}).get("volume", 0) or 0)
    except Exception:
        return 0

# Simple function to parse and display conditions
def parse_market_conditions(description: str) -> list[str]:
    """Parse the market description to extract individual conditions"""
    conditions = []
    if not description:
        return conditions
    
    # Split by commas and clean up
    parts = [part.strip() for part in description.split(',')]
    for part in parts:
        if part:  # Skip empty parts
            conditions.append(part)
    
    return conditions

# Enhanced market snapshot that includes conditions
async def market_snapshot_with_conditions(ticker: str):
    """
    Returns market data with parsed conditions
    """
    try:
        market_data = await fetch_market(ticker)
        market_info = market_data.get("market", {})
        
        title = market_info.get("title", "")
        description = market_info.get("description", "")
        status = market_info.get("status", "unknown")
        
        # If market is closed, return None prices
        if status in ["closed", "expired", "settled"]:
            return None, None, None, 0, status, []
        
        # Get prices and volume
        yes_ask = market_info.get("yes_ask")
        yes_bid = market_info.get("yes_bid") 
        last_price = market_info.get("last_price")
        volume = market_info.get("volume", 0)
        
        # Calculate prices
        yes_price = None
        if yes_ask is not None:
            yes_price = yes_ask / 100.0
        elif yes_bid is not None:
            yes_price = yes_bid / 100.0
        elif last_price is not None:
            yes_price = last_price / 100.0
            
        no_price = round(1 - yes_price, 4) if yes_price is not None else None
        
        # Parse conditions from description
        conditions = parse_market_conditions(description)
        
        return yes_price, no_price, last_price, volume, status, conditions
        
    except Exception as e:
        print(f"Error in market_snapshot_with_conditions for {ticker}: {e}")
        # Fallback to basic snapshot
        yes, no, last, vol, status = await market_snapshot(ticker)
        return yes, no, last, vol, status, []

# -------------- Slash Commands --------------
def maybe_guilds_decorator():
    # If GUILD_IDS is empty, do not restrict (global commands)
    return (app_commands.guilds(*GUILDS)) if GUILDS else (lambda f: f)

@maybe_guilds_decorator()
@tree.command(name="help", description="How to use this bot (simple guide)")
async def help_cmd(interaction: discord.Interaction):
    txt = (
        "**Kalshi Helper — Command Guide**\n"
        "• **/find <words>** — Search open markets by words (try: `sports`, `politics`, or a player/team).\n"
        "• **/movers** — Show active markets with volume & price (YES in cents).\n"
        "• **/watch <TICKER> [threshold]** — Watch a market; optional alert when YES ≤ threshold (e.g., `0.35`).\n"
        "• **/unwatch <TICKER>** — Stop watching a market.\n"
        "• **/list** — List all watched markets for this server.\n"
        "• **/vol <link-or-ticker> [minutes] [limit]** — Show volume and conditions for one market or multiple related markets.\n\n"
        "_Tips:_\n"
        "- Use `/vol KXTICKER` for a single market with conditions\n"
        "- Use `/vol nfl` to see all NFL markets with volumes\n"
        "- Prices show like `65¢` (i.e., $0.65)"
    )
    await interaction.response.send_message(txt, ephemeral=True)

@maybe_guilds_decorator()
@tree.command(name="find", description="Find Kalshi markets by keyword (friendly to 'sports', 'politics', etc.)")
@app_commands.describe(query="Keywords for market title search (e.g., 'sports', 'lakers', 'election')")
async def find_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    mkts = await fetch_markets_open()
    q = query.lower().strip()

    # Expand topic hints for non-expert queries
    needles = [q]
    for topic, hints in TOPIC_HINTS.items():
        if q == topic:
            needles = hints
            break

    hits = []
    for m in mkts:
        title = (m.get("title", "") or "").lower()
        if any(n in title for n in needles):
            ticker = m.get("ticker")
            yes, _, _, vol, status = await market_snapshot(ticker)
            # Only include markets that are actually trading
            if yes is not None:  # This filters out closed markets
                hits.append((m.get("title",""), ticker, yes, vol))
            if len(hits) >= 12:
                break

    if not hits:
        await interaction.followup.send(f"No **open** markets matched `{query}`.")
        return

    out = []
    for title, tick, y, vol in hits:
        out.append(f"**{title}**\n`{tick}` • YES≈{to_cents_str(y)} • vol={vol}")
    for chunk in chunk_lines(out):
        await interaction.followup.send(chunk)

@maybe_guilds_decorator()
@tree.command(name="movers", description="Show top open markets by rough activity")
async def movers_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    mkts = await fetch_markets_open()
    scored = []
    for m in mkts:
        ticker, title = m.get("ticker"), m.get("title", "")
        vol = int(m.get("volume", 0) or 0)
        yes, no_price, _, _, status = await market_snapshot(ticker)
        # Skip closed markets
        if yes is None:
            continue
        rng = abs(yes - (1 - no_price)) if (yes is not None and no_price is not None) else 0
        score = vol + rng * 100
        scored.append((score, title, ticker, yes, no_price, vol))
    top = sorted(scored, key=lambda x: x[0], reverse=True)[:20]
    if not top:
        await interaction.followup.send("No **open** markets found.")
        return
    out = []
    for _, title, ticker, yes, no_price, vol in top:
        out.append(f"**{title}**\n`{ticker}` • YES≈{to_cents_str(yes)} • NO≈{to_cents_str(no_price)} • vol={vol}")
    for chunk in chunk_lines(out):
        await interaction.followup.send(chunk)

@maybe_guilds_decorator()
@tree.command(name="watch", description="Watch a market; optional YES threshold to alert")
@app_commands.describe(
    ticker="Kalshi market ticker (or paste a link)",
    threshold="Alert when YES ≤ threshold (0.00–1.00, e.g., 0.35)"
)
async def watch_cmd(interaction: discord.Interaction, ticker: str, threshold: float | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    gid = str(interaction.guild_id)
    if not gid or gid == "None":
        await interaction.followup.send("Run this in a **server** (not DMs) so I can send channel alerts.")
        return

    # Allow links here too
    tick = extract_ticker(ticker) or ticker.upper().strip()

    w = watches.get(gid, {})
    w[tick] = {"threshold": threshold}
    watches[gid] = w
    save_json(WATCH_FILE, watches)

    msg = f"Watching `{tick}`"
    if threshold is not None:
        msg += f" (alert when YES ≤ {round(threshold*100):.0f}¢)"
    await interaction.followup.send(msg)

@maybe_guilds_decorator()
@tree.command(name="unwatch", description="Remove a watched market")
@app_commands.describe(ticker="Kalshi market ticker (or paste link)")
async def unwatch_cmd(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    gid = str(interaction.guild_id)
    if not gid or gid == "None":
        await interaction.followup.send("Run this in a **server** (not DMs) so I can find that guild's watchlist.")
        return

    tick = extract_ticker(ticker) or ticker.upper().strip()
    w = watches.get(gid, {})
    removed = w.pop(tick, None)
    watches[gid] = w
    save_json(WATCH_FILE, watches)

    if removed:
        await interaction.followup.send(f"Removed `{tick}` from watchlist.")
    else:
        await interaction.followup.send(f"`{tick}` wasn't being watched.")

@maybe_guilds_decorator()
@tree.command(name="list", description="List watched markets for this server")
async def list_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    gid = str(interaction.guild_id)
    if not gid or gid == "None":
        await interaction.followup.send("Run this in a **server** (not DMs).")
        return

    w = watches.get(gid, {})
    if not w:
        await interaction.followup.send("No watches set.")
        return

    lines = []
    for t, cfg in w.items():
        thr = cfg.get("threshold")
        thr_text = f" (YES ≤ {round(thr*100):.0f}¢)" if isinstance(thr, (int, float)) else ""
        lines.append(f"`{t}`{thr_text}")
    for chunk in chunk_lines(lines, max_chars=1800, sep="\n"):
        await interaction.followup.send(chunk)

# Volume command that shows only volume and conditions
@maybe_guilds_decorator()
@tree.command(name="vol", description="Show volume and conditions for markets")
@app_commands.describe(
    link_or_ticker="Kalshi link, ticker, OR search term (e.g., 'nfl', 'election')",
    minutes="Look-back window in minutes for volume changes (default 15)",
    limit="Maximum number of markets to show (default 10)"
)
async def vol_cmd(interaction: discord.Interaction, link_or_ticker: str, minutes: int = 15, limit: int = 10):
    await interaction.response.defer(ephemeral=True, thinking=True)

    input_text = link_or_ticker.strip()
    
    # Check if it's a specific ticker
    ticker = extract_ticker(input_text) or input_text.upper()
    
    # If it looks like a specific ticker (starts with KX), show just that one
    if ticker.startswith('KX'):
        # Use the enhanced snapshot with conditions
        yes_now, no_now, last_price, volume, status, conditions = await market_snapshot_with_conditions(ticker)
        
        if volume == 0 or status in ["closed", "expired", "settled"]:
            await interaction.followup.send(f"❌ `{ticker}` is closed or has no volume (status: {status})")
            return
        
        # Record current price for history (even though we're not showing it)
        record_price(ticker, yes_now)
        
        # Build response - ONLY VOLUME AND CONDITIONS
        lines = [
            f"**{ticker}**",
            f"• Volume: {volume}",
        ]
        
        # Add conditions if we have them
        if conditions:
            lines.append("")
            lines.append("**Conditions:**")
            for condition in conditions:
                lines.append(f"• {condition}")
        
        # Volume change calculation (simplified)
        arr = histories.get(ticker, [])
        if len(arr) > 1:
            lines.append("")
            lines.append(f"• Active market with trading history")
        else:
            lines.append("")
            lines.append(f"• New market activity")
        
        await interaction.followup.send("\n".join(lines))
    
    else:
        # Search term mode - show multiple related markets with only volume
        search_term = input_text.lower()
        mkts = await fetch_markets_open()
        
        hits = []
        for m in mkts:
            title = (m.get("title", "") or "").lower()
            if search_term in title:
                ticker = m.get("ticker")
                yes, no_price, last_price, volume, status, conditions = await market_snapshot_with_conditions(ticker)
                # Only include markets that have volume
                if volume > 0:
                    # Record for history
                    record_price(ticker, yes)
                    hits.append((title, ticker, volume, conditions))
                if len(hits) >= limit:
                    break
        
        if not hits:
            await interaction.followup.send(f"No **active** markets found for `{search_term}`.")
            return
        
        # Sort by volume (highest first)
        hits.sort(key=lambda x: x[2], reverse=True)
        
        lines = [f"**Markets related to '{search_term}'** (showing {len(hits)} of {limit})"]
        
        for title, ticker, volume, conditions in hits:
            lines.append(f"**{title}**")
            lines.append(f"`{ticker}` • Volume: {volume}")
            
            # Show first few conditions if available
            if conditions and len(conditions) > 0:
                condition_preview = ", ".join(conditions[:3])  # Show first 3 conditions
                if len(conditions) > 3:
                    condition_preview += f" ... (+{len(conditions)-3} more)"
                lines.append(f"Conditions: {condition_preview}")
            
            lines.append("")  # Empty line between markets
        
        for chunk in chunk_lines(lines):
            await interaction.followup.send(chunk)

# -------------- Alerts --------------
async def alert_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            for gid, w in list(watches.items()):
                guild = client.get_guild(int(gid)) if gid.isdigit() else None
                if not guild or not guild.text_channels:
                    continue
                chan = guild.text_channels[0]
                for ticker, cfg in list(w.items()):
                    thr = cfg.get("threshold")
                    yes, _, _, _, status = await market_snapshot(ticker)
                    # Skip if market is closed
                    if yes is None:
                        continue
                    # log rolling history for /vol
                    record_price(ticker, yes)
                    if thr is None:
                        continue
                    if yes is not None and yes <= thr:
                        await chan.send(f"🔔 `{ticker}` YES={to_cents_str(yes)} (≤ {round(thr*100):.0f}¢)")
                        w[ticker]["threshold"] = None
                        save_json(WATCH_FILE, watches)
            await asyncio.sleep(60)
        except Exception as e:
            print("alert_loop error:", e)
            await asyncio.sleep(5)

# -------------- Global error handler --------------
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    from discord.app_commands import CommandInvokeError
    msg = "Something went wrong."
    if isinstance(error, CommandInvokeError) and getattr(error, "original", None):
        msg = f"Command error: {type(error.original).__name__}: {error.original}"
    else:
        msg = f"{type(error).__name__}: {error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"⚠️ {msg}", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)
    except Exception:
        pass

# -------------- Startup --------------
@client.event
async def on_ready():
    try:
        if GUILDS:
            # Guild-scoped
            for gid in GUILD_IDS:
                await tree.sync(guild=discord.Object(id=gid))
        else:
            # Global commands
            await tree.sync()
        print(f"Slash commands synced. (scope={'guild' if GUILDS else 'global'})")
        print(f"🤖 {client.user} is online and ready!")
    except Exception as e:
        print("Slash sync failed:", e)

# -------------- Health server (optional) --------------
async def start_health_server():
    """
    Tiny web server for hosts that require a listening port (Replit Web/Render Web).
    Only runs if PORT env var is set.
    """
    port = os.environ.get("PORT")
    if not port:
        return

    async def handle(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/healthz", handle)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=int(port))
    await site.start()
    print(f"🩺 Health server listening on :{port}")

# -------------- Run --------------
async def main():
    await start_health_server()
    async with client:
        await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
    except Exception:
        pass
    asyncio.run(main())