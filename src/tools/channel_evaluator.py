"""Telegram Channel Evaluator — discover free option signal channels and rank them.

Usage:
  python -m src.tools.channel_evaluator discover   # search & list public channels
  python -m src.tools.channel_evaluator fetch       # fetch last 30 days messages
  python -m src.tools.channel_evaluator evaluate    # parse signals + backtest + rank
  python -m src.tools.channel_evaluator all         # run all steps

Requires: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE in env.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, Chat

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = Path(os.path.dirname(__file__)).parent.parent / "data" / "channel_eval"

# ---------------------------------------------------------------------------
# Step 1: Discover public channels
# ---------------------------------------------------------------------------
SEARCH_KEYWORDS = [
    "nifty option signals",
    "banknifty free signals",
    "sensex option trading",
    "index option calls free",
    "nifty banknifty calls",
    "option trading signals free",
    "nifty premium calls",
    "banknifty intraday signals",
    "sensex intraday free",
    "nifty CE PE signals",
    "option buy sell signals",
    "index option intraday",
    "free nifty tips",
    "banknifty option tips",
    "nifty futures signals",
]


async def discover_channels(client: TelegramClient) -> list[dict]:
    """Search Telegram for public option signal channels."""
    seen_ids = set()
    channels = []

    for kw in SEARCH_KEYWORDS:
        try:
            result = await client(SearchRequest(q=kw, limit=20))
            for chat in result.chats:
                if not isinstance(chat, Channel):
                    continue
                if chat.id in seen_ids:
                    continue
                if chat.megagroup:  # skip groups, want channels only
                    continue
                seen_ids.add(chat.id)
                channels.append({
                    "id": chat.id,
                    "title": chat.title,
                    "username": chat.username,
                    "participants_count": getattr(chat, "participants_count", None),
                })
                print(f"  Found: {chat.title} (@{chat.username}, id={chat.id}, members={getattr(chat, 'participants_count', '?')})")
        except Exception as exc:
            print(f"  Search '{kw}' failed: {exc}")
        await asyncio.sleep(2)  # rate limit

    # Also scan existing dialogs for channels we're already in
    print("\nScanning existing dialogs...")
    async for dialog in client.iter_dialogs(limit=300):
        if not isinstance(dialog.entity, Channel):
            continue
        if dialog.entity.id in seen_ids:
            continue
        if dialog.entity.megagroup:
            continue
        title = (dialog.entity.title or "").lower()
        # Check if title suggests option trading
        keywords = ["option", "nifty", "banknifty", "sensex", "trading", "signal",
                     "call", "put", "index", "f&o", "intraday", "premium"]
        if any(k in title for k in keywords):
            seen_ids.add(dialog.entity.id)
            channels.append({
                "id": dialog.entity.id,
                "title": dialog.entity.title,
                "username": dialog.entity.username,
                "participants_count": getattr(dialog.entity, "participants_count", None),
            })
            print(f"  Dialog: {dialog.entity.title} (id={dialog.entity.id})")

    # Save
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / "channels.json"
    json.dump(channels, open(out, "w"), indent=2, default=str)
    print(f"\nFound {len(channels)} channels -> {out}")
    return channels


# ---------------------------------------------------------------------------
# Step 2: Fetch messages from discovered channels (last 30 days)
# ---------------------------------------------------------------------------
async def fetch_messages(client: TelegramClient, days: int = 30):
    """Fetch recent messages from all discovered channels."""
    channels_file = DATA_DIR / "channels.json"
    if not channels_file.exists():
        print("No channels.json found. Run 'discover' first.")
        return

    channels = json.load(open(channels_file))
    cutoff = datetime.now(IST) - timedelta(days=days)
    messages_dir = DATA_DIR / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)

    for ch in channels:
        cid = ch["id"]
        title = ch["title"]
        out_file = messages_dir / f"{cid}.json"

        # Skip if already fetched today
        if out_file.exists():
            existing = json.load(open(out_file))
            if existing and existing[0].get("_fetched_date") == datetime.now(IST).strftime("%Y-%m-%d"):
                print(f"  Skip {title} (already fetched today)")
                continue

        print(f"  Fetching: {title} (id={cid})...")
        msgs = []
        try:
            entity = await client.get_entity(cid)
            async for msg in client.iter_messages(entity, limit=500):
                if msg.date.astimezone(IST) < cutoff:
                    break
                text = (msg.text or "").strip()
                if not text:
                    continue
                msgs.append({
                    "id": msg.id,
                    "ts": msg.date.astimezone(IST).isoformat(),
                    "text": text,
                })
            msgs.insert(0, {"_fetched_date": datetime.now(IST).strftime("%Y-%m-%d"), "_channel": title})
            json.dump(msgs, open(out_file, "w"), indent=1, ensure_ascii=False)
            print(f"    -> {len(msgs)-1} messages")
        except Exception as exc:
            print(f"    -> ERROR: {exc}")
        await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# Step 3: Parse signals from messages
# ---------------------------------------------------------------------------
# Known index names
INDICES = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTYBANK", "BANKEX"}

# Common stock option symbols
STOCK_SYMS = {
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "TATAMOTORS",
    "MARUTI", "BAJFINANCE", "BHARTIARTL", "ITC", "LT", "AXISBANK", "KOTAKBANK",
    "HINDUNILVR", "WIPRO", "HCLTECH", "ADANIENT", "TATASTEEL", "JSWSTEEL",
    "M&M", "SUNPHARMA", "DRREDDY", "CIPLA", "APOLLOHOSP", "TITAN", "MCX",
    "AMBER", "LAURUSLABS", "ZOMATO", "PAYTM", "PNB", "COALINDIA", "POWERGRID",
    "NTPC", "ONGC", "BPCL", "IOC", "GAIL", "HEROMOTOCO", "BAJAJ-AUTO",
    "EICHERMOT", "TVSMOTOR", "INDUSINDBK", "FEDERALBNK", "IDFCFIRSTB",
    "CANBK", "BANKBARODA", "RECLTD", "PFC", "NHPC", "IRFC", "HAL", "BEL",
    "DIXON", "DEEPAKNTR", "TRENT", "GODREJCP", "DABUR", "VOLTAS",
}

ALL_SYMBOLS = INDICES | STOCK_SYMS


def parse_signal(text: str, msg_ts: str) -> dict | None:
    """Try to parse an option trading signal from message text.

    Handles common formats:
      BUY NIFTY 25700 CE ABOVE 245 TGT 280/320/380 SL 160
      SENSEX 83900 CE ABOVE 220 TARGET 300/400/500 SL 170
      BANKNIFTY 66600 CE BUY ABOVE 427 TARGET 475,520,600 SL 380
      NIFTY 25700CE ABOVE 245 TARGET :- 280/320/380 SL : 160
    """
    raw = text
    # Normalize
    t = text.upper().replace("**", "").replace("*", "")
    t = re.sub(r"[🎯🚀✅❤️‍🔥💎📈📊🔥💵🕯✔️💯🎁💲🟣⚡️🤝🔮🔺📹🕞🕟🕠🕡🕢🕣👌👍😁🐎💰🏆]", "", t)
    t = re.sub(r"\s+", " ", t).strip()

    # Must have CE or PE
    if " CE " not in t and " PE " not in t and " CE" not in t and " PE" not in t:
        # Try "CALL" / "PUT"
        t = t.replace(" CALL ", " CE ").replace(" PUT ", " PE ")
        t = re.sub(r"\bCALL\b", "CE", t)
        t = re.sub(r"\bPUT\b", "PE", t)

    if "CE" not in t and "PE" not in t:
        return None

    # Find symbol
    symbol = None
    for sym in ALL_SYMBOLS:
        if re.search(r"\b" + re.escape(sym) + r"\b", t):
            symbol = sym
            break
    if not symbol:
        return None

    # Find strike price (number after symbol, before CE/PE)
    # Pattern: SYMBOL <strike> CE/PE or SYMBOL<strike>CE/PE
    strike_match = re.search(
        re.escape(symbol) + r"\s*(\d{3,6})\s*(CE|PE)", t
    )
    if not strike_match:
        return None
    strike = float(strike_match.group(1))
    option_type = strike_match.group(2)

    # Find trigger price (ABOVE xxx)
    trigger = None
    above_match = re.search(r"ABOVE\s*[:=-]?\s*(\d+\.?\d*)", t)
    if above_match:
        trigger = float(above_match.group(1))

    # Also try "BUY.*NEAR.*<price>" or "BUY AT <price>"
    if trigger is None:
        buy_match = re.search(r"(?:BUY|NEAR)\s+(?:AT\s+)?(\d{2,5}\.?\d*)", t)
        if buy_match:
            trigger = float(buy_match.group(1))

    if trigger is None:
        return None

    # Find SL
    sl = None
    sl_match = re.search(r"(?:SL|STOP\s*LOSS)\s*[:=-]?\s*(\d+\.?\d*)", t)
    if sl_match:
        sl = float(sl_match.group(1))

    # Find targets
    targets = []
    # Pattern: TARGET/TGT :- xxx/xxx/xxx or xxx,xxx,xxx or xxx - xxx - xxx
    tgt_match = re.search(r"(?:TARGET|TGT|T)\s*[:=-]?\s*([\d\s/,\-+.]+)", t)
    if tgt_match:
        tgt_str = tgt_match.group(1)
        # Extract all numbers
        nums = re.findall(r"(\d+\.?\d*)", tgt_str)
        targets = [float(n) for n in nums if float(n) > trigger * 0.5]  # sanity check

    if not targets:
        return None

    return {
        "symbol": symbol,
        "strike": strike,
        "option_type": option_type,
        "trigger": trigger,
        "sl": sl,
        "targets": targets[:5],  # max 5 targets
        "ts": msg_ts,
        "time": msg_ts[11:16] if len(msg_ts) > 16 else msg_ts,
        "raw": raw[:200],
    }


def parse_all_channels() -> dict[int, list[dict]]:
    """Parse signals from all fetched channel messages."""
    messages_dir = DATA_DIR / "messages"
    if not messages_dir.exists():
        print("No messages fetched. Run 'fetch' first.")
        return {}

    channels = json.load(open(DATA_DIR / "channels.json"))
    id_to_title = {ch["id"]: ch["title"] for ch in channels}

    results = {}
    for f in sorted(messages_dir.glob("*.json")):
        cid = int(f.stem)
        msgs = json.load(open(f))
        if not msgs:
            continue
        meta = msgs[0] if msgs and "_fetched_date" in msgs[0] else {}
        title = meta.get("_channel", id_to_title.get(cid, str(cid)))

        signals = []
        for m in msgs:
            if "_fetched_date" in m:
                continue
            sig = parse_signal(m["text"], m["ts"])
            if sig:
                sig["msg_id"] = m["id"]
                signals.append(sig)

        if signals:
            results[cid] = signals
            print(f"  {title}: {len(signals)} signals parsed from {len(msgs)-1} messages")
        else:
            print(f"  {title}: 0 signals (skipping)")

    # Save parsed signals
    out = DATA_DIR / "parsed_signals.json"
    json.dump(results, open(out, "w"), indent=1, default=str)
    print(f"\nSaved parsed signals -> {out}")
    return results


# ---------------------------------------------------------------------------
# Step 4: Backtest signals against Upstox historical candles
# ---------------------------------------------------------------------------
def _load_upstox_token():
    token_path = Path(os.path.dirname(__file__)).parent.parent / "data" / ".upstox_session.json"
    data = json.load(open(token_path))
    return data["access_token"]


def _find_instrument_key(fo_instruments: list, symbol: str, strike: float, ot: str) -> str | None:
    seg_map = {"SENSEX": "BSE_FO", "BANKEX": "BSE_FO"}
    seg = seg_map.get(symbol, "NSE_FO")
    today = datetime.now(IST).date()
    best = None
    for inst in fo_instruments:
        if inst.get("segment") != seg:
            continue
        if (inst.get("asset_symbol") or "").upper() != symbol:
            continue
        if inst.get("instrument_type") != ot:
            continue
        if abs(float(inst.get("strike_price", 0)) - strike) > 0.01:
            continue
        exp = inst.get("expiry")
        if isinstance(exp, (int, float)):
            exp_d = datetime.fromtimestamp(exp / 1000, tz=IST).date()
        elif isinstance(exp, str):
            try:
                exp_d = date.fromisoformat(exp[:10])
            except ValueError:
                continue
        else:
            continue
        if exp_d < today:
            continue
        if best is None or exp_d < best[0]:
            best = (exp_d, inst.get("instrument_key", ""))
    return best[1] if best else None


def _get_candles(token: str, key: str, sig_date: str) -> list:
    """Get 1-min candles for a specific date."""
    import requests
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}

    # For today, use intraday endpoint
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    if sig_date == today_str:
        url = f"https://api.upstox.com/v2/historical-candle/intraday/{key}/1minute"
        r = requests.get(url, headers=headers, timeout=15)
    else:
        # Historical endpoint: /v2/historical-candle/{key}/{interval}/{to_date}/{from_date}
        url = f"https://api.upstox.com/v2/historical-candle/{key}/1minute/{sig_date}/{sig_date}"
        r = requests.get(url, headers=headers, timeout=15)

    data = r.json()
    candles = data.get("data", {}).get("candles", [])
    return sorted(candles, key=lambda c: c[0])


def backtest_signal(token: str, fo_instruments: list, sig: dict, buffer: int = 10) -> dict | None:
    """Backtest a single signal against candle data. Returns result dict."""
    key = _find_instrument_key(fo_instruments, sig["symbol"], sig["strike"], sig["option_type"])
    if not key:
        return None

    sig_date = sig["ts"][:10]
    sig_time = sig["ts"][11:16]

    try:
        candles = _get_candles(token, key, sig_date)
    except Exception:
        return None

    if not candles:
        return None

    trigger = sig["trigger"]
    sl = sig.get("sl")
    targets = sig["targets"]

    # Find entry
    entry_t = None
    for c in candles:
        ts = c[0][11:16]
        if ts >= sig_time and c[2] >= trigger:
            entry_t = ts
            break

    if not entry_t:
        return {"status": "NO_ENTRY", "signal": sig}

    # Simulate with trailing SL (TGT - buffer)
    current_sl = sl if sl else trigger * 0.7  # fallback SL if not given
    current_tgt_idx = 0
    exit_price = None
    exit_time = None
    exit_reason = None

    for c in candles:
        ts = c[0][11:16]
        if ts < entry_t:
            continue
        h, l = c[2], c[3]

        while current_tgt_idx < len(targets) and h >= targets[current_tgt_idx]:
            new_sl = targets[current_tgt_idx] - buffer
            if new_sl > current_sl:
                current_sl = new_sl
            current_tgt_idx += 1

        if l <= current_sl:
            exit_price = current_sl
            exit_time = ts
            exit_reason = "ORIGINAL_SL" if current_tgt_idx == 0 else f"TRAIL_TGT{current_tgt_idx}"
            break

    if exit_price is None:
        last = candles[-1]
        exit_price = last[4]
        exit_time = last[0][11:16]
        exit_reason = "EOD"

    pnl_pts = exit_price - trigger
    return {
        "status": "TRADED",
        "entry": trigger,
        "entry_time": entry_t,
        "exit": exit_price,
        "exit_time": exit_time,
        "exit_reason": exit_reason,
        "pnl_pts": round(pnl_pts, 2),
        "tgt_reached": current_tgt_idx,
        "signal": sig,
    }


def evaluate_all():
    """Backtest all parsed signals and produce a channel scorecard."""
    signals_file = DATA_DIR / "parsed_signals.json"
    if not signals_file.exists():
        print("No parsed signals. Run parse step first.")
        return

    all_signals = json.load(open(signals_file))
    channels = json.load(open(DATA_DIR / "channels.json"))
    id_to_title = {str(ch["id"]): ch["title"] for ch in channels}

    token = _load_upstox_token()

    # Load FO instruments
    fo_cache = sorted(Path(os.path.dirname(__file__)).parent.parent.glob("data/instruments_fo_*.json"))
    if fo_cache:
        fo = json.load(open(fo_cache[-1]))
        print(f"Loaded {len(fo)} F&O instruments from {fo_cache[-1].name}")
    else:
        from src.broker.upstox_client import load_fo_instruments
        fo = load_fo_instruments()

    scorecard = {}
    all_results = {}

    for cid_str, signals in all_signals.items():
        title = id_to_title.get(cid_str, cid_str)
        print(f"\n{'='*50}")
        print(f"Evaluating: {title} ({len(signals)} signals)")
        print(f"{'='*50}")

        results = []
        wins = losses = no_entry = errors = 0

        for i, sig in enumerate(signals):
            # Rate limit Upstox API
            if i > 0 and i % 5 == 0:
                time.sleep(1)

            r = backtest_signal(token, fo, sig, buffer=10)
            if r is None:
                errors += 1
                continue
            results.append(r)

            if r["status"] == "NO_ENTRY":
                no_entry += 1
                continue

            if r["pnl_pts"] > 0:
                wins += 1
            else:
                losses += 1

            sym = f"{sig['symbol']} {int(sig['strike'])} {sig['option_type']}"
            status = "WIN" if r["pnl_pts"] > 0 else "LOSS"
            print(f"  {sig['ts'][:10]} {sym}: {status} {r['pnl_pts']:+.0f} pts ({r['exit_reason']})")

        total = wins + losses
        win_rate = (wins / total * 100) if total > 0 else 0
        avg_pnl = sum(r["pnl_pts"] for r in results if r["status"] == "TRADED") / total if total > 0 else 0
        total_pnl = sum(r["pnl_pts"] for r in results if r["status"] == "TRADED")

        scorecard[cid_str] = {
            "title": title,
            "total_signals": len(signals),
            "traded": total,
            "wins": wins,
            "losses": losses,
            "no_entry": no_entry,
            "errors": errors,
            "win_rate": round(win_rate, 1),
            "avg_pnl_pts": round(avg_pnl, 1),
            "total_pnl_pts": round(total_pnl, 1),
        }
        all_results[cid_str] = results

        print(f"\n  Traded: {total} | Wins: {wins} | Losses: {losses} | Win Rate: {win_rate:.0f}%")
        print(f"  Avg P&L: {avg_pnl:+.1f} pts | Total P&L: {total_pnl:+.1f} pts")

    # Save results
    json.dump(scorecard, open(DATA_DIR / "scorecard.json", "w"), indent=2)
    json.dump(all_results, open(DATA_DIR / "backtest_results.json", "w"), indent=1, default=str)

    # Print final ranking
    print("\n" + "=" * 70)
    print("CHANNEL SCORECARD (ranked by win rate)")
    print("=" * 70)
    ranked = sorted(scorecard.values(), key=lambda x: (-x["win_rate"], -x["total_pnl_pts"]))
    print(f"{'Channel':<35} {'Signals':>7} {'Traded':>6} {'Wins':>4} {'WR%':>5} {'Avg P&L':>8} {'Total':>8}")
    print("-" * 70)
    for s in ranked:
        print(f"{s['title'][:34]:<35} {s['total_signals']:>7} {s['traded']:>6} {s['wins']:>4} {s['win_rate']:>5.0f}% {s['avg_pnl_pts']:>+7.0f} {s['total_pnl_pts']:>+7.0f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def run(mode: str):
    API_ID = int(os.environ["TELEGRAM_API_ID"])
    API_HASH = os.environ["TELEGRAM_API_HASH"]
    PHONE = os.environ["TELEGRAM_PHONE"]

    if mode in ("discover", "fetch", "all"):
        client = TelegramClient("data/telegram_reader", API_ID, API_HASH)
        await client.start(phone=PHONE)
        me = await client.get_me()
        print(f"Logged in as {me.first_name} (phone={me.phone})\n")

        if mode in ("discover", "all"):
            print("=== STEP 1: Discovering channels ===")
            await discover_channels(client)

        if mode in ("fetch", "all"):
            print("\n=== STEP 2: Fetching messages ===")
            await fetch_messages(client)

        await client.disconnect()

    if mode in ("parse", "evaluate", "all"):
        print("\n=== STEP 3: Parsing signals ===")
        parse_all_channels()

    if mode in ("evaluate", "all"):
        print("\n=== STEP 4: Backtesting & Ranking ===")
        evaluate_all()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    asyncio.run(run(mode))
