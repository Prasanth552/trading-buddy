# Trading Buddy

An automated NSE/BSE trading assistant. It reads Indian + world news, analyses a
watchlist of index charts through the trading day, sends Telegram alerts with an
8-section analysis, and (only when explicitly enabled) auto-executes small,
defined-risk option trades via Zerodha Kite.

> **Safety first.** `MODE` defaults to `PAPER`. No real order is ever placed
> until you set `MODE="LIVE"` in `config.py` **and** pass `--confirm-live` at
> runtime. The safety guardrails (kill switch, stop-loss on every entry, daily
> limits) are hard requirements — see the build spec §10.

## Scope

- **NSE/BSE only.** Foreign-market trading is out of scope (FEMA/LRS).
- Single retail trader, personal use. Capital: ₹50,000 → option buying and
  defined-risk spreads only (option selling / futures need far more margin).
- All times are **Asia/Kolkata (IST)**. Market hours: **09:15–15:30, Mon–Fri**,
  excluding NSE holidays.

## Requirements

- Python 3.11+
- Zerodha account + Kite Connect (with data subscription)
- Anthropic API key
- Telegram bot token + chat ID
- (For live algo trading) a VPS with a static IP

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows PowerShell
# source .venv/bin/activate   # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure secrets
copy .env.example .env        # then fill in the values
#   KITE_API_KEY, KITE_API_SECRET, KITE_TOTP_SECRET (optional),
#   ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# 4. Review non-secret settings
#   edit config.py — WATCHLIST, capital, risk limits, news feeds
```

## Self-test (Phase 0)

Runs offline — creates the SQLite DB, prints the config, and confirms the IST
clock + market-hours logic. No broker / LLM / Telegram calls.

```bash
python main.py --selftest
```

Expected: `Self-test PASSED.`

## Commands

```bash
python main.py --init            # FIRST-RUN setup + health check (DB, keys, all connections)
python main.py --selftest        # offline checks (DB, config, clock, market hours)
python main.py --login           # Kite login; caches today's access token
python main.py --snapshot        # technical snapshot per watchlist symbol  (Phase 1)
python main.py --news            # poll feeds, tag with the LLM, store        (Phase 2)
python main.py --signals         # combine technicals + news, emit signals    (Phase 3)
python main.py --analyze         # 8-section LLM analysis per symbol (console) (Phase 4)
python main.py --alert           # push 8-section analysis + signals to Telegram
python main.py --bot             # run the Telegram command bot (blocks)
python main.py --telegram-test   # send a test message to confirm delivery
python main.py --chat-id         # print your Telegram chat ID
python main.py --paper-trade     # scan + size + place orders (PAPER simulated) (Phase 5)
python main.py --run-once        # one full orchestration cycle now (news→signals→exec)
python main.py --monitor         # close open PAPER positions on stop/target; realize P&L
python main.py --eod             # send the end-of-day summary now
python main.py                   # start the unattended scheduler (blocks; Ctrl-C to stop)
```

### Running the service

`python main.py` (no flags) starts the **APScheduler loop**: it runs a full cycle
every 15 min during market hours (09:15–15:30 IST, Mon–Fri, skipping NSE holidays),
sends an **EOD summary** at 15:31, and does a pre-open token check at 09:05. Alerts
are low-noise — the 8-section analysis + a trade alert fire only when a signal triggers.

Run `python main.py --login` first each morning (or set `KITE_USER_ID` / `KITE_PASSWORD`
/ `KITE_TOTP_SECRET` in `.env` for opt-in automated login).

### For a non-technical daily user

- **`Start Trading Buddy.bat`** — double-click launcher (no terminal/commands needed).
  Make a Desktop shortcut from it. With auto-login set, this is the only daily step.
- **`Daily Login (manual).bat`** — fallback login helper if not using auto-login.
- **`MOM-GUIDE.md`** — a plain-language daily guide written for the end user.
- **`GO-LIVE-CHECKLIST.md`** — operator steps for the launcher, auto-login, VPS, the
  LIVE flip, and the kill-switch test. Work through it in order before risking real money.

> Real orders require **both** `MODE="LIVE"` in `config.py` **and** the `--confirm-live`
> flag at runtime. Without both, every order is simulated. Guardrails (no order without a
> stop, daily-loss kill switch, max trades/day, min lot size) are always enforced.

## Daily routine (once running)

1. **Pre-open:** start the service; refresh the Kite token (login + 2FA).
2. **09:15–15:30 IST:** loop — news → technicals → signals → alerts →
   (auto-)orders within the guardrails.
3. **Post-close:** EOD summary to Telegram (trades, P&L, what fired and why).
4. **Weekly:** review the log — win rate, avg win vs loss, drawdown, rule adherence.

## Build phases

| Phase | Scope | Status |
|------:|-------|--------|
| 0 | Setup, scaffold, DB, logging, market calendar, `--selftest` | ✅ done |
| 1 | Market data + technical engine (pivots, RSI, candlesticks) | ✅ done (live-verified) |
| 2 | News engine (RSS + announcements, LLM tagging) | ✅ done (live-verified) |
| 3 | Signal engine (combine technicals + news, low-risk rules) | ✅ done (live-verified) |
| 4 | Telegram alerts (8-section analysis) + commands | ✅ done (live-verified — alerts land on phone; commands via `--bot`) |
| 5 | Execution module (PAPER/LIVE, stop-loss, guardrails) | ✅ done — real option sizing, protective stop, guardrails, **exit simulation** (stop/target → realized P&L), kill switch exercisable |
| 6 | Go live (small) — kill switch, EOD summary | ⏳ (gated on approval + VPS) |
| 7 | Web dashboard (status, trades, P&L, pause/resume) | ✅ built (`--dashboard`); deploy guide in `DEPLOY.md` |
| Ongoing | Reliability: scheduler loop, EOD summary, token refresh | ✅ done (live-verified `--run-once` / `--eod`; opt-in TOTP auto-login) |

## Project layout

```
trading-buddy/
├── config.py              # non-secret settings
├── main.py                # entry point + scheduler loop
├── requirements.txt
├── .env.example           # copy to .env (git-ignored)
├── data/                  # SQLite DB
├── logs/                  # rotating logs
└── src/
    ├── broker/            # Kite auth, data, orders, daily token refresh
    ├── data/              # market data + indicators
    ├── news/              # feeds + LLM analyzer
    ├── signals/           # signal engine + Signal model
    ├── execution/         # executor + guardrails
    ├── notify/            # Telegram bot
    ├── llm/               # Anthropic wrapper
    ├── storage/           # SQLite schema + helpers
    └── utils/             # logging, market calendar
```

## Disclaimer

This software places (or simulates) trades and can lose money. Use `PAPER` mode
until results justify going live, start at the minimum lot size, and never
disable the safety guardrails. You are responsible for your own trades and for
compliance with SEBI's retail-algo framework and Zerodha's API terms.
