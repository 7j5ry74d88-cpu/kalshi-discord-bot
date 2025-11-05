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
    """Format a 0–1 price as cents like '65¢', or '—' if missing."""
    if val is None:
        return "—"
    try:
        return f"{round(float(val)*100):.0f}¢"
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

    async def pull(status):
        cursor = None
        while True:
            params = {"limit": limit}
            if status:
                params["status"] = status
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

    for status in ("open", "active", None):
        await pull(status)
        if results:
            break

    return results

async def fetch_market(ticker):
    return await kalshi_get(f"/markets/{ticker}")

async def fetch_orderbook(ticker):
    return await kalshi_get(f"/markets/{ticker}/orderbook")

# Improved snapshot with fallbacks and last trade
async def market_snapshot(ticker: str):
    """
    Returns (yes_price, no_bid, last_yes) in 0–1 floats with smart fallbacks:
      - yes_price: best YES we can infer (YES bid, or 1-NO bid, or last trade)
      - no_bid: best NO bid if present, else inferred (1-YES), else None
      - last_yes: last traded YES (if available), else None
    """
    yes_bid = None
    no_bid  = None
    last_yes = None

    try:
        ob = await fetch_orderbook(ticker)
        yside = ob.get("yes", []) or []
        nside = ob.get("no", []) or []
        if yside:
            yes_bid = max(level.get("price", 0) for level in yside) / 100.0
        if nside:
            no_bid  = max(level.get("price", 0) for level in nside) / 100.0
    except Exception:
        pass

    try:
        m = await fetch_market(ticker)
        lp = (m.get("market", {}) or {}).get("last_price")
        if isinstance(lp, int):
            last_yes = lp / 100.0
    except Exception:
        pass

    # YES fallback chain
    yes_price = None
    if yes_bid is not None:
        yes_price = yes_bid
    elif no_bid is not None:
        yes_price = round(1 - no_bid, 2)
    elif last_yes is not None:
        yes_price = last_yes

    # NO fallback chain
    if no_bid is None:
        if yes_bid is not None:
            no_bid = round(1 - yes_bid, 2)
        elif last_yes is not None:
            no_bid = round(1 - last_yes, 2)

    return yes_price, no_bid, last_yes

def market_volume_from_payload(market_payload: dict) -> int:
    try:
        return int((market_payload.get("market", {}) or {}).get("volume", 0) or 0)
    except Exception:
        return 0

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
        "• **/vol <link-or-ticker> [minutes]** — Show current YES (cents), volume, and change vs ~N minutes ago.\n\n"
        "_Tips:_ You can paste a Kalshi link anywhere a ticker is accepted. Prices show like `65¢` (i.e., $0.65)."
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
            yes, _, _ = await market_snapshot(ticker)
            hits.append((m.get("title",""), ticker, yes, int(m.get("volume",0) or 0)))
            if len(hits) >= 12:
                break

    if not hits:
        await interaction.followup.send(f"No open markets matched `{query}`.")
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
        yes, no_bid, _ = await market_snapshot(ticker)
        rng = abs(yes - (1 - no_bid)) if (yes is not None and no_bid is not None) else 0
        score = vol + rng * 100
        scored.append((score, title, ticker, yes, no_bid, vol))
    top = sorted(scored, key=lambda x: x[0], reverse=True)[:20]
    if not top:
        await interaction.followup.send("No open markets found.")
        return
    out = []
    for _, title, ticker, yes, no_bid, vol in top:
        out.append(f"**{title}**\n`{ticker}` • YES≈{to_cents_str(yes)} • NO(bid)≈{to_cents_str(no_bid)} • vol={vol}")
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
        await interaction.followup.send("Run this in a **server** (not DMs) so I can find that guild’s watchlist.")
        return

    tick = extract_ticker(ticker) or ticker.upper().strip()
    w = watches.get(gid, {})
    removed = w.pop(tick, None)
    watches[gid] = w
    save_json(WATCH_FILE, watches)

    if removed:
        await interaction.followup.send(f"Removed `{tick}` from watchlist.")
    else:
        await interaction.followup.send(f"`{tick}` wasn’t being watched.")

@maybe_guilds_decorator()
@tree.command(name="list", description="List watched markets for this server")
async def list_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    gid = str(interaction.guild_id)
    if not gid or gid == "None":
        await interaction.followup.send("Run this in a **server** (not DMs)." )
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

@maybe_guilds_decorator()
@tree.command(name="vol", description="Show current YES (cents), volume, and Δ vs N minutes ago")
@app_commands.describe(
    link_or_ticker="Paste a Kalshi link or the market ticker",
    minutes="Look-back window in minutes (default 15)"
)
async def vol_cmd(interaction: discord.Interaction, link_or_ticker: str, minutes: int = 15):
    await interaction.response.defer(ephemeral=True, thinking=True)

    ticker = extract_ticker(link_or_ticker) or link_or_ticker.upper().strip()
    # Current snapshot
    yes_now, no_now, _ = await market_snapshot(ticker)

    # Volume read
    vol = 0
    try:
        mp = await fetch_market(ticker)
        vol = market_volume_from_payload(mp)
    except Exception:
        pass

    # Record current into history (so next queries have data)
    record_price(ticker, yes_now)

    # Find prior yes at or before cutoff
    cutoff = int(time.time()) - minutes*60
    arr = histories.get(ticker, [])
    yes_prev = None
    # Walk from newest to oldest to find the first <= cutoff
    for ts, y in reversed(arr):
        if ts <= cutoff:
            yes_prev = y
            break

    lines = [f"**{ticker}** • volume={vol} • YES≈{to_cents_str(yes_now)} • NO(bid)≈{to_cents_str(no_now)}"]
    if yes_prev is None or yes_now is None:
        lines.append(f"Δ — (no captured quotes ≥ {minutes}m; try again later)")
    else:
        delta_c = round((yes_now - yes_prev)*100)
        lines.append(f"Δ {delta_c:+.0f}¢ over {minutes}m (from {to_cents_str(yes_prev)} → {to_cents_str(yes_now)})")

    await interaction.followup.send("\n".join(lines))

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
                    yes, _, _ = await market_snapshot(ticker)
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
