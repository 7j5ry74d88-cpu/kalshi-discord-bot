# bot.py
import os, json, asyncio, re
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
    raise SystemExit("Missing DISCORD_TOKEN environment variable.")

# Optional proxy (e.g., Cloudflare Worker) to bypass WAF on Replit
PROXY_BASE = os.environ.get("PROXY_BASE")  # e.g., https://your-worker.workers.dev/api

GUILD_IDS = [1429395255786082315, 1433848533580124303]
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
    return max(level.get("price", 0) for level in book_side) / 100.0

def yes_from_no(no_price):
    return round(1 - no_price, 2) if no_price is not None else None

async def market_snapshot(ticker):
    try:
        ob = await fetch_orderbook(ticker)
        yes_bid = best_price_from_book(ob.get("yes", []))
        no_bid = best_price_from_book(ob.get("no", []))
        yes_price = yes_bid if yes_bid is not None else yes_from_no(no_bid)
        return yes_price, no_bid
    except Exception:
        try:
            m = await fetch_market(ticker)
            last_yes = m.get("market", {}).get("last_price")
            return (last_yes / 100.0 if isinstance(last_yes, int) else None), None
        except Exception:
            return None, None

# -------------- Slash Commands --------------
@app_commands.guilds(*GUILDS)
@tree.command(name="ping", description="Health check")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! ✅", ephemeral=True)

@app_commands.guilds(*GUILDS)
@tree.command(name="find", description="Find Kalshi markets by keyword")
@app_commands.describe(query="Keyword to search in market titles")
async def find_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    mkts = await fetch_markets_open()
    q, hits = query.lower().strip(), []
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
    out = [f"**{t}**\n`{tick}` • YES≈${y:.2f}" if y else f"**{t}**\n`{tick}`"
           for t, tick, y in hits]
    for chunk in chunk_lines(out):
        await interaction.followup.send(chunk)

@app_commands.guilds(*GUILDS)
@tree.command(name="movers", description="Show top open markets by rough activity")
async def movers_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    mkts = await fetch_markets_open()
    scored = []
    for m in mkts:
        ticker, title = m.get("ticker"), m.get("title", "")
        vol = m.get("volume", 0) or 0
        yes, no_bid = await market_snapshot(ticker)
        rng = abs(yes - (1 - no_bid)) if (yes is not None and no_bid is not None) else 0
        score = vol + rng * 100
        scored.append((score, title, ticker, yes, vol))
    top = sorted(scored, key=lambda x: x[0], reverse=True)[:20]
    if not top:
        await interaction.followup.send("No open markets found.")
        return
    out = [f"**{title}**\n`{ticker}` • YES≈${(yes if yes is not None else 0):.2f if yes is not None else '—'} • vol={vol}"
           for _, title, ticker, yes, vol in top]
    for chunk in chunk_lines(out):
        await interaction.followup.send(chunk)

# --- Diagnostics for Replit / WAF ---
@app_commands.guilds(*GUILDS)
@tree.command(name="diag", description="Show Kalshi API diagnostics")
async def diag_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    report = []
    try:
        for status in ("open", "active", None):
            try:
                params = {"limit": 25}
                if status:
                    params["status"] = status
                data = await kalshi_get("/markets", params)
                mkts = data.get("markets", []) or []
                sample = [m.get("title", "")[:70] for m in mkts[:3]]
                report.append(f"{status or 'no status'}: {len(mkts)} • ex: {sample}")
            except Exception as e:
                report.append(f"{status or 'no status'}: ERROR {e!r}")
        await interaction.followup.send(" | ".join(report))
    except Exception as e:
        await interaction.followup.send(f"API error: {e!r}")

@app_commands.guilds(*GUILDS)
@tree.command(name="probe", description="Inspect raw /markets response")
async def probe_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    reports = []
    for base in KALSHI_BASES:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0),
                headers=HEADERS,
                follow_redirects=True,
                http2=False,
            ) as s:
                r = await s.get(f"{base}/markets", params={"limit": 5})
                ctype = r.headers.get("Content-Type", "")
                snip = r.text[:400].replace("\n", " ")
                reports.append(f"{base} → {r.status_code} • {ctype} • {len(r.text)} bytes • {snip}")
        except Exception as e:
            reports.append(f"{base} → ERROR {e!r}")
    for chunk in chunk_lines(reports, max_chars=1800, sep="\n\n"):
        await interaction.followup.send(chunk)

# -------------- Watch / Unwatch / List --------------
@app_commands.guilds(*GUILDS)
@tree.command(name="watch", description="Watch a market; optional YES threshold to alert")
@app_commands.describe(
    ticker="Kalshi market ticker (e.g., KX...)",
    threshold="Alert when YES ≤ threshold (0.00–1.00, e.g., 0.35)"
)
async def watch_cmd(interaction: discord.Interaction, ticker: str, threshold: float | None = None):
    # First-line log to confirm handler invocation
    print(f"[watch] invoked by user={interaction.user.id} in guild={interaction.guild_id} ticker={ticker} thr={threshold}")
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
        msg += f" (alert when YES ≤ ${threshold:.2f})"
    await interaction.followup.send(msg)

# Fallback duplicate to bypass any cached-stale command shapes
@app_commands.guilds(*GUILDS)
@tree.command(name="watch2", description="Watch a market (fallback) with optional YES threshold")
@app_commands.describe(
    ticker="Kalshi market ticker (e.g., KX...)",
    threshold="Alert when YES ≤ threshold (0.00–1.00, e.g., 0.35)"
)
async def watch2_cmd(interaction: discord.Interaction, ticker: str, threshold: float | None = None):
    print(f"[watch2] invoked by user={interaction.user.id} in guild={interaction.guild_id} ticker={ticker} thr={threshold}")
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        gid = str(interaction.guild_id)
        if not gid or gid == "None":
            await interaction.followup.send("Run this in a **server** (not DMs) so I can send channel alerts.")
            return

        w = watches.get(gid, {})
        w[ticker.upper()] = {"threshold": threshold}
        watches[gid] = w
        save_watches(watches)

        msg = f"Watching `{ticker.upper()}`"
        if isinstance(threshold, (int, float)):
            msg += f" (alert when YES ≤ ${threshold:.2f})"
        await interaction.followup.send(msg, ephemeral=True)
        print(f"[watch2] ok guild={gid} ticker={ticker.upper()} thr={threshold}")
    except Exception as e:
        print("[watch2] error:", repr(e))
        try:
            await interaction.followup.send(f"⚠️ Error: {e}", ephemeral=True)
        except Exception:
            pass

@app_commands.guilds(*GUILDS)
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

@app_commands.guilds(*GUILDS)
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
        thr_text = f" (YES ≤ ${thr:.2f})" if isinstance(thr, (int, float)) else ""
        lines.append(f"`{t}`{thr_text}")

    for chunk in chunk_lines(lines, max_chars=1800, sep="\n"):
        await interaction.followup.send(chunk)

# -------------- Free-form helpers & commands --------------
def parse_first_ticker(s: str) -> str | None:
    for tok in re.findall(r'[A-Z0-9][A-Z0-9_.-]{6,}', (s or "").upper()):
        if tok.startswith('KX') and '-' in tok:
            return tok
    return None

def parse_threshold(s: str) -> float | None:
    m = re.search(r'(?:threshold\s*[:=]?\s*)?([01](?:\.\d+)?|\.\d+)', s, flags=re.I)
    if not m:
        return None
    try:
        val = float(m.group(1))
        if 0 < val <= 1.0:
            return val
        return None
    except Exception:
        return None

@app_commands.guilds(*GUILDS)
@tree.command(name="watch_raw", description="Free-form watch: paste ticker and optional threshold")
@app_commands.describe(text='Examples: "ticker: KX... threshold: 0.35" | "KX... 0.35" | "0.4 KX..."')
async def watch_raw_cmd(interaction: discord.Interaction, text: str):
    await interaction.response.defer(ephemeral=True, thinking=True)

    gid = str(interaction.guild_id)
    if not gid or gid == "None":
        await interaction.followup.send("Run this in a **server** (not DMs) so I can send channel alerts.")
        return

    ticker = parse_first_ticker(text)
    if not ticker:
        await interaction.followup.send("Couldn’t find a valid ticker in your text. Paste something like `KX...-...`")
        return

    thr = parse_threshold(text)
    w = watches.get(gid, {})
    w[ticker.upper()] = {"threshold": thr}
    watches[gid] = w
    save_watches(watches)

    if thr is not None:
        await interaction.followup.send(f"Watching `{ticker.upper()}` (alert when YES ≤ ${thr:.2f}).")
    else:
        await interaction.followup.send(f"Watching `{ticker.upper()}` (no threshold set). Use `/watch` to add one later.")

@app_commands.guilds(*GUILDS)
@tree.command(name="unwatch_raw", description="Free-form unwatch: paste text that contains a ticker")
@app_commands.describe(text='Example: "unwatch KXNFL...-BUFJCOOK4-90"')
async def unwatch_raw_cmd(interaction: discord.Interaction, text: str):
    await interaction.response.defer(ephemeral=True, thinking=True)

    gid = str(interaction.guild_id)
    if not gid or gid == "None":
        await interaction.followup.send("Run this in a **server** (not DMs) so I can find that guild’s watchlist.")
        return

    ticker = parse_first_ticker(text)
    if not ticker:
        await interaction.followup.send("Couldn’t find a valid ticker in your text. Paste something like `KX...-...`")
        return

    w = watches.get(gid, {})
    removed = w.pop(ticker.upper(), None)
    watches[gid] = w
    save_watches(watches)

    if removed:
        await interaction.followup.send(f"Removed `{ticker.upper()}` from watchlist.")
    else:
        await interaction.followup.send(f"`{ticker.upper()}` wasn’t being watched.")

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
                    yes, _ = await market_snapshot(ticker)
                    if yes is not None and thr is not None and yes <= thr:
                        await chan.send(f"🔔 `{ticker}` YES=${yes:.2f} (≤ ${thr:.2f})")
                        w[ticker]["threshold"] = None
                        save_watches(watches)
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
        # HARD RESYNC: clear then re-add guild commands to avoid stale cached shapes
        for gid in GUILD_IDS:
            await tree.sync(guild=discord.Object(id=gid))
            tree.clear_commands(guild=discord.Object(id=gid))
        await asyncio.sleep(0.5)
        for gid in GUILD_IDS:
            await tree.sync(guild=discord.Object(id=gid))

        for gid in GUILD_IDS:
            cmds = await tree.fetch_commands(guild=discord.Object(id=gid))
            print(f"Guild {gid} commands → {len(cmds)}: {[c.name for c in cmds]}")

        print(f"🤖 {client.user} is online and ready!")
    except Exception as e:
        print("Slash sync failed:", e)

# -------------- Run --------------
async def main():
    async with client:
        await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
    except Exception:
        pass
    asyncio.run(main())
