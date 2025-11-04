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

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN environment variable.")

# Optional proxy (e.g., Cloudflare Worker) to bypass WAF on Replit
PROXY_BASE = os.environ.get("PROXY_BASE")  # e.g., https://your-worker.workers.dev/api

# If you want commands to work in ANY server, leave GUILD_IDS empty so they register globally.
GUILD_IDS = []  # e.g., [1234567890]
GUILDS = [] if not GUILD_IDS else [discord.Object(id=i) for i in GUILD_IDS]

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

watches = load_watches()

# In‑memory price history for /vol (reset on restart)
PRICE_HISTORY = {}  # {ticker: [(ts, yes_float or None), ...]}
MAX_HISTORY_LEN = 500

# -------------- Discord Client --------------
intents = discord.Intents.default()

class MyClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.bg_task = None

    async def setup_hook(self):
        # Start background alert loop after client is ready
        self.bg_task = asyncio.create_task(alert_loop())

client = MyClient(intents=intents)
tree = client.tree

# -------------- Helpers --------------
def fmt_cents(val):
    """
    Convert a YES price in dollars (0.65) into '65¢'.
    None -> '—'
    """
    if val is None:
        return "—"
    try:
        return f"{int(round(float(val)*100))}¢"
    except Exception:
        return "—"

def chunk_lines(lines, max_chars=1800, sep="\n\n"):
    chunk, length = [], 0
    for line in lines:
        add = (sep if chunk else "") + line
        if length + len(add) > max_chars:
            yield sep.join(chunk)
            chunk, length = [line], len(line)
        else:
            chunk.append(line)
            length += len(add)
    if chunk:
        yield sep.join(chunk)

def record_history(ticker, price):
    arr = PRICE_HISTORY.setdefault(ticker, [])
    arr.append((time.time(), price))
    if len(arr) > MAX_HISTORY_LEN:
        del arr[: len(arr) - MAX_HISTORY_LEN]

def first_sample_older_than(ticker, minutes):
    arr = PRICE_HISTORY.get(ticker) or []
    cutoff = time.time() - minutes*60
    older = [pair for pair in arr if pair[0] <= cutoff and pair[1] is not None]
    return older[0] if older else None

def parse_ticker_from_input(txt: str) -> str | None:
    """
    Extract KX... ticker from a pasted link or text.
    """
    if not txt:
        return None
    s = txt.strip()
    # If it's a link, look for a KX... token inside it
    m = re.search(r'(KX[A-Z0-9][A-Z0-9\-]*)', s.upper())
    if m:
        return m.group(1)
    # Otherwise, if user just pasted the ticker
    if s.upper().startswith("KX") and '-' in s:
        return s.upper()
    return None

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
                    # Non-JSON (likely HTML challenge) → try next base
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

    # try progressively looser status filters
    for status in ("open", "active", None):
        await pull(status)
        if results:
            break

    return results

async def fetch_market(ticker):
    return await kalshi_get(f"/markets/{ticker}")

async def fetch_orderbook(ticker):
    return await kalshi_get(f"/markets/{ticker}/orderbook")

def best_price_from_book(book_side):
    if not book_side:
        return None
    try:
        return max(level.get("price", 0) for level in book_side) / 100.0
    except Exception:
        return None

def yes_from_no(no_price):
    return round(1 - no_price, 2) if no_price is not None else None

async def market_snapshot(ticker, *, record=True):
    """
    Return (yes_price, no_bid) in dollars. Try orderbook, then last_price fallback.
    Treat last_price == 0 with no orderbook as None (illiquid).
    """
    yes_price, no_bid = None, None
    try:
        ob = await fetch_orderbook(ticker)
        yes_price = best_price_from_book(ob.get("yes", []))
        no_bid = best_price_from_book(ob.get("no", []))
        if yes_price is None and no_bid is not None:
            yes_price = yes_from_no(no_bid)
    except Exception:
        pass

    if yes_price is None:
        try:
            m = await fetch_market(ticker)
            last_yes = m.get("market", {}).get("last_price")
            # Convert cents→dollars
            if isinstance(last_yes, int):
                last_yes = last_yes / 100.0
            # If last_yes is 0.0 and there is no book, treat as None (no signal)
            if (last_yes == 0 or last_yes is None) and no_bid is None:
                yes_price = None
            else:
                yes_price = last_yes
        except Exception:
            pass

    if record:
        record_history(ticker, yes_price)

    return yes_price, no_bid

# -------------- Slash Commands --------------
def add_commands_scope(deco):
    # Use guild scope if GUILD_IDS set; otherwise register globally
    return (lambda f: app_commands.guilds(*GUILDS)(f)) if GUILDS else (lambda f: f)

@add_commands_scope
@tree.command(name="help", description="Show bot commands")
async def help_cmd(interaction: discord.Interaction):
    txt = (
        "**Kalshi Bot — Commands**\n"
        "/help — Show this help.\n"
        "/find <keyword> — Find open markets by keyword; shows YES price in ¢.\n"
        "/movers — Top active markets (filters out illiquid where possible).\n"
        "/watch <ticker> [threshold] — Watch a market; optional YES ≤ threshold alert.\n"
        "/unwatch <ticker> — Remove a watched market.\n"
        "/list — List watched tickers for this server.\n"
        "/vol <link-or-ticker> [minutes] — Show current YES/NO and price change over N minutes.\n"
        "\nTips: paste any Kalshi market link into /vol. Use thresholds like 0.35."
    )
    await interaction.response.send_message(txt, ephemeral=True)

@add_commands_scope
@tree.command(name="find", description="Find Kalshi markets by keyword")
@app_commands.describe(query="Keyword to search in market titles")
async def find_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    mkts = await fetch_markets_open()
    q, hits = query.lower().strip(), []
    for m in mkts:
        title = m.get("title", "")
        if q in (title or "").lower():
            ticker = m.get("ticker")
            yes, _ = await market_snapshot(ticker)
            hits.append((title, ticker, yes))
            if len(hits) >= 10:
                break
    if not hits:
        await interaction.followup.send(f"No open markets matched `{query}`.")
        return
    out = []
    for t, tick, y in hits:
        out.append(f"**{t}**\n`{tick}` • YES≈{fmt_cents(y)}")
    for chunk in chunk_lines(out):
        await interaction.followup.send(chunk)

@add_commands_scope
@tree.command(name="movers", description="Show top open markets by rough activity")
async def movers_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    mkts = await fetch_markets_open()
    scored = []
    for m in mkts:
        ticker, title = m.get("ticker"), m.get("title", "")
        vol_total = m.get("volume", 0) or 0
        # Try to get a current snapshot
        yes, no_bid = await market_snapshot(ticker, record=False)
        # Price range score only if both sides exist
        price_rng = 0.0
        if yes is not None and no_bid is not None:
            price_rng = abs(yes - (1 - no_bid))
        # Filter: keep if volume or visible liquidity
        keep = (vol_total > 0) or (yes is not None and no_bid is not None)
        if not keep:
            continue
        score = (vol_total) + price_rng * 200
        scored.append((score, title, ticker, yes, vol_total, no_bid))
    top = sorted(scored, key=lambda x: x[0], reverse=True)[:20]
    if not top:
        await interaction.followup.send("No liquid markets found.")
        return
    out = []
    for _, title, ticker, yes, vol_total, no_bid in top:
        no_str = fmt_cents(no_bid) if no_bid is not None else "—"
        out.append(f"**{title}**\n`{ticker}` • YES≈{fmt_cents(yes)} • NO(bid)≈{no_str} • vol={vol_total}")
    for chunk in chunk_lines(out):
        await interaction.followup.send(chunk)

@add_commands_scope
@tree.command(name="watch", description="Watch a market; optional YES threshold to alert")
@app_commands.describe(
    ticker="Kalshi market ticker (e.g., KX...)",
    threshold="Alert when YES ≤ threshold (0.00–1.00, e.g., 0.35)"
)
async def watch_cmd(interaction: discord.Interaction, ticker: str, threshold: float | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)

    gid = str(interaction.guild_id)
    if not gid or gid == "None":
        await interaction.followup.send("Run this in a **server** (not DMs) so I can send channel alerts.")
        return

    w = watches.get(gid, {})
    w[ticker.upper()] = {"threshold": threshold}
    watches[gid] = w
    save_watches(watches)

    msg = f"Watching `{ticker.upper()}`"
    if threshold is not None:
        msg += f" (alert when YES ≤ {fmt_cents(threshold)})"
    await interaction.followup.send(msg)

@add_commands_scope
@tree.command(name="unwatch", description="Remove a watched market")
@app_commands.describe(ticker="Kalshi market ticker (e.g., KX...)")
async def unwatch_cmd(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(ephemeral=True, thinking=True)

    gid = str(interaction.guild_id)
    if not gid or gid == "None":
        await interaction.followup.send("Run this in a **server** (not DMs) so I can find that guild’s watchlist.")
        return

    w = watches.get(gid, {})
    removed = w.pop(ticker.upper(), None)
    watches[gid] = w
    save_watches(watches)

    if removed:
        await interaction.followup.send(f"Removed `{ticker.upper()}` from watchlist.")
    else:
        await interaction.followup.send(f"`{ticker.upper()}` wasn’t being watched.")

@add_commands_scope
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
        thr_text = f" (YES ≤ {fmt_cents(thr)})" if isinstance(thr, (int, float)) else ""
        lines.append(f"`{t}`{thr_text}")

    for chunk in chunk_lines(lines, max_chars=1800, sep="\n"):
        await interaction.followup.send(chunk)

@add_commands_scope
@tree.command(name="vol", description="Show price & change over N minutes for a market link/ticker")
@app_commands.describe(link_or_ticker="Paste a Kalshi market link or a KX... ticker",
                       minutes="Lookback window in minutes (default 15)")
async def vol_cmd(interaction: discord.Interaction, link_or_ticker: str, minutes: int = 15):
    await interaction.response.defer(ephemeral=True, thinking=True)

    ticker = parse_ticker_from_input(link_or_ticker)
    if not ticker:
        await interaction.followup.send("I couldn't find a `KX...` ticker in that. Paste a market link or ticker.")
        return

    # Take a fresh snapshot (and record to history)
    try:
        yes, no_bid = await market_snapshot(ticker, record=True)
        # Also fetch market meta for volume
        meta = await fetch_market(ticker)
        vol_total = meta.get("market", {}).get("volume", 0) or 0
    except httpx.HTTPStatusError as e:
        await interaction.followup.send(f"Couldn’t fetch `{ticker}`: {e}")
        return
    except Exception as e:
        await interaction.followup.send(f"Error reading `{ticker}`: {e}")
        return

    # Compute delta
    older = first_sample_older_than(ticker, minutes)
    if older and older[1] is not None and yes is not None:
        delta_cents = int(round((yes - older[1]) * 100))
        sign = "+" if delta_cents >= 0 else ""
        delta_str = f"{sign}{delta_cents}¢ over {minutes}m"
    else:
        delta_str = f"— (no captured quotes ≥ {minutes}m ago; try again later)"

    no_str = fmt_cents(no_bid) if no_bid is not None else "—"
    msg = (
        f"**Market**\n"
        f"`{ticker}` • volume={vol_total} • YES≈{fmt_cents(yes)} • NO(bid)≈{no_str}\n"
        f"Δ {delta_str}"
    )
    await interaction.followup.send(msg)

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
                    if thr is None:
                        continue
                    yes, _ = await market_snapshot(ticker, record=False)
                    if yes is not None and thr is not None and yes <= thr:
                        await chan.send(f"🔔 `{ticker}` YES={fmt_cents(yes)} (≤ {fmt_cents(thr)})")
                        w[ticker]["threshold"] = None
                        save_watches(watches)
            await asyncio.sleep(60)
        except Exception as e:
            print("alert_loop error:", e)
            await asyncio.sleep(5)

# -------------- Startup --------------
@client.event
async def on_ready():
    try:
        if GUILDS:
            # Guild-scoped commands
            for gid in GUILD_IDS:
                await tree.sync(guild=discord.Object(id=gid))
            for gid in GUILD_IDS:
                cmds = await tree.fetch_commands(guild=discord.Object(id=gid))
                print(f"Guild {gid} commands → {len(cmds)}: {[c.name for c in cmds]}")
        else:
            # Global commands
            await tree.sync()
            cmds = await tree.fetch_commands()
            print(f"Global commands → {len(cmds)}: {[c.name for c in cmds]}")
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
    # Start optional health server first (only if PORT is set)
    await start_health_server()

    # Then run the Discord client
    async with client:
        await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
    except Exception:
        pass
    asyncio.run(main())
