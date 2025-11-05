
from pathlib import Path
import os
import re
import json
import time
from typing import Optional, Tuple, Dict, Any, List

import httpx
from discord import app_commands
import discord

# ---------------- Storage ----------------
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
QUOTES_FILE = DATA_DIR / "quotes.json"

def _load_quotes() -> Dict[str, List[Dict[str, Any]]]:
    try:
        if QUOTES_FILE.exists():
            return json.loads(QUOTES_FILE.read_text())
    except Exception:
        pass
    return {}

def _save_quotes(cache: Dict[str, List[Dict[str, Any]]]):
    try:
        QUOTES_FILE.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        print("Failed to save quotes:", e)

_QUOTES = _load_quotes()

# ---------------- Config ----------------
PROXY_BASE = os.environ.get("PROXY_BASE")  # Optional CF Worker, etc.
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

# ---------------- URL/Ticker parsing ----------------
_TICKER_RE = re.compile(r"(KX[A-Z0-9_.-]{6,})", re.I)

def extract_ticker(link_or_ticker: str) -> Optional[str]:
    """
    Accepts a raw ticker (KX...) or a kalshi.com market URL.
    Returns uppercased ticker if found.
    """
    s = (link_or_ticker or "").strip()
    m = _TICKER_RE.search(s.upper())
    if m:
        return m.group(1).upper()
    return None

# ---------------- Networking ----------------
async def kalshi_get(path: str, params: Dict[str, Any] | None = None) -> Any:
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

async def fetch_market(ticker: str) -> Dict[str, Any]:
    return await kalshi_get(f"/markets/{ticker}")

async def fetch_orderbook(ticker: str) -> Dict[str, Any]:
    return await kalshi_get(f"/markets/{ticker}/orderbook")

# ---------------- Pricing helpers ----------------
def price_to_cents(val: Optional[float]) -> str:
    if val is None:
        return "—"
    try:
        return f"{round(float(val) * 100):.0f}¢"
    except Exception:
        return "—"

def best_price_from_book(book_side: List[Dict[str, Any]]) -> Optional[float]:
    if not book_side:
        return None
    return max(level.get("price", 0) for level in book_side) / 100.0

def yes_from_no(no_price: Optional[float]) -> Optional[float]:
    return round(1 - no_price, 2) if no_price is not None else None

# ---------------- Snapshot & delta ----------------
def _record_snapshot(ticker: str, yes_est: Optional[float], ts: Optional[int] = None):
    if yes_est is None:
        return
    ts = ts or int(time.time())
    series = _QUOTES.get(ticker, [])
    series.append({"ts": ts, "yes": float(yes_est)})
    _QUOTES[ticker] = series[-500:]
    _save_quotes(_QUOTES)

def _delta_over_minutes(ticker: str, minutes: int) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    series = _QUOTES.get(ticker, [])
    if not series:
        return (None, None, None, None)
    now = int(time.time())
    cutoff = now - minutes * 60
    older_points = [p for p in series if p["ts"] <= now and p["ts"] >= cutoff]
    if len(older_points) < 1:
        first = series[0]
        last = series[-1]
        window = max(1, int((last["ts"] - first["ts"]) / 60))
        delta = round((last["yes"] - first["yes"]) * 100)
        return (delta, round(first["yes"] * 100), round(last["yes"] * 100), window)
    first = older_points[0]
    last = series[-1]
    window = max(1, int((last["ts"] - first["ts"]) / 60))
    delta = round((last["yes"] - first["yes"]) * 100)
    return (delta, round(first["yes"] * 100), round(last["yes"] * 100), window)

# ---------------- Main snapshot ----------------
async def market_snapshot(ticker: str) -> Dict[str, Any]:
    yes_bid = no_bid = last_yes = vol = None
    try:
        ob = await fetch_orderbook(ticker)
        yes_bid = best_price_from_book(ob.get("yes", []))
        no_bid = best_price_from_book(ob.get("no", []))
    except Exception:
        pass
    try:
        m = await fetch_market(ticker)
        vol = (m.get("market") or {}).get("volume")
        last = (m.get("market") or {}).get("last_price")
        if isinstance(last, int):
            last_yes = last / 100.0
    except Exception:
        pass

    yes_est = yes_bid if yes_bid is not None else (yes_from_no(no_bid) if no_bid is not None else last_yes)
    _record_snapshot(ticker, yes_est)

    return {
        "yes_bid": yes_bid,
        "no_bid": no_bid,
        "last_yes": last_yes,
        "volume": vol,
        "yes_est": yes_est,
    }

# ---------------- /vol command wiring ----------------
async def _vol_cmd(interaction: discord.Interaction, link_or_ticker: str, minutes: int = 15):
    await interaction.response.defer(ephemeral=True, thinking=True)

    ticker = extract_ticker(link_or_ticker)
    if not ticker:
        await interaction.followup.send("I couldn't find a valid Kalshi ticker in that input.", ephemeral=True)
        return

    try:
        snap = await market_snapshot(ticker)
    except Exception as e:
        await interaction.followup.send(f"Couldn't fetch `{ticker}`: {e}", ephemeral=True)
        return

    yes_c = price_to_cents(snap["yes_est"])
    no_c = price_to_cents(snap["no_bid"])
    vol = snap["volume"] if isinstance(snap["volume"], int) else (snap["volume"] or 0)

    dlt, frm, to, win = _delta_over_minutes(ticker, minutes)
    if dlt is None:
        delta_line = "Δ — (no captured quotes yet; try again later)"
    else:
        sign = "+" if dlt > 0 else ""
        delta_line = f"Δ {sign}{dlt}¢ over {win}m (from {frm}¢ → {to}¢)"

    if snap["yes_bid"] is not None:
        src = "(bid)"
    elif snap["no_bid"] is not None:
        src = "(1−NO)"
    elif snap["last_yes"] is not None:
        src = "(last)"
    else:
        src = ""

    head = f"**{ticker}** • volume={vol}\nYES≈{yes_c} {src} • NO(bid)≈{no_c}"
    await interaction.followup.send(f"{head}\n{delta_line}", ephemeral=True)

def add_vol_command(tree: app_commands.CommandTree, guild_ids: Optional[List[int]] = None):
    vol_cmd = app_commands.Command(
        name="vol",
        description="Show volume, current price (in ¢), and change over N minutes",
        callback=_vol_cmd,
    )
    vol_cmd.parameters[0].name = "link_or_ticker"
    vol_cmd.parameters[0].description = "Kalshi link or ticker (e.g., KX...)"
    vol_cmd.parameters[1].name = "minutes"
    vol_cmd.parameters[1].description = "Lookback window in minutes (default 15)"
    vol_cmd.parameters[1].default = 15

    if guild_ids:
        for gid in guild_ids:
            tree.add_command(vol_cmd, guild=discord.Object(id=gid))
    else:
        tree.add_command(vol_cmd)  # global

__all__ = [
    "add_vol_command",
    "extract_ticker",
    "market_snapshot",
    "price_to_cents",
]
