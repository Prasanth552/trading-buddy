# Go-Live Checklist (for the operator — not for the daily user)

Work through this **in order**. Do not skip the PAPER-proving or the kill-switch test.
Real money is only at risk after the very last step.

---

## 0. Prerequisites before even thinking about LIVE

- [ ] Ran in **PAPER** for at least a couple of weeks; reviewed the EOD summaries.
- [ ] Win rate / avg win vs avg loss / drawdown look acceptable to you.
- [ ] You understand that with ₹50k capital most NIFTY-lot trades won't fit the
      ₹500 risk budget (the sizer rejects them) — confirm the strategy still makes sense.
- [ ] Verified the NSE holiday list and lot sizes in `config.py` against the official
      NSE circular for the current year.

---

## 1. One-click launcher for the daily user (Setup A — free, laptop)

- [ ] Create a Desktop shortcut: right-click **`Start Trading Buddy.bat`** →
      *Show more options* → *Send to* → *Desktop (create shortcut)* → rename it
      **"Start Trading Buddy"**. (Optional: right-click the shortcut → *Properties* →
      *Change Icon* for something friendly.)
- [ ] Same for **`Daily Login (manual).bat`** → name it **"Daily Login"** (skip if using auto-login).
- [ ] (Optional) **Auto-start on boot:** press `Win+R`, type `shell:startup`, Enter,
      and drop a copy of the *Start Trading Buddy* shortcut into that folder.
- [ ] (Optional) **Prevent sleep** during the day: Settings → System → Power →
      Screen & sleep → set "sleep" to *Never* (while on power).
- [ ] Hand the daily user **`MOM-GUIDE.md`** (or print it).

---

## 2. Auto-login (removes the morning token paste)

> ⚠️ This stores the Zerodha **password + TOTP secret** in `.env`. Only enable it on a
> machine you trust (ideally the VPS in step 3, not a shared laptop). It automates Kite's
> web login, which can occasionally break — if it does, the daily user can't fix it, you will.

- [ ] Add to `.env`:
      ```
      KITE_USER_ID=YOUR_ZERODHA_ID
      KITE_PASSWORD=YOUR_ZERODHA_PASSWORD
      KITE_TOTP_SECRET=THE_TOTP_SETUP_KEY   # the secret shown when enabling 2FA (not the 6-digit code)
      ```
- [ ] Test it: `python -c "from src.broker.session import automated_login; automated_login(); print('AUTO-LOGIN OK')"`
- [ ] Confirm it caches a token and `python main.py --snapshot` works without `--login`.
- [ ] Only after it works reliably for a few days, rely on it for the daily user.

---

## 3. VPS (Setup B — true hands-off; required for LIVE)

SEBI requires a **static IP** for automated order placement; a VPS provides one and runs 24/7.

- [ ] Rent a small VPS (e.g. an India-region instance, ~₹400–700/mo). Note its **static IP**.
- [ ] Whitelist that IP in the Kite Connect developer console (app settings).
- [ ] Install Python 3.11+, copy the project, create the venv, `pip install -r requirements.txt`.
- [ ] Copy `.env` (with auto-login creds) to the VPS — keep file permissions locked down.
- [ ] Run it as a background service (Windows: Task Scheduler at logon; Linux: `systemd` unit
      or `tmux`/`screen`) so it survives reboots and restarts on crash.
- [ ] Confirm `--snapshot` and `--run-once` work from the VPS.

---

## 4. Test the kill switch BEFORE going live (still PAPER)

- [ ] Force a daily loss in the DB and confirm trading halts:
      ```
      python -c "from src.storage import db; from src.utils import market_calendar as mc; \
      d=mc.now_ist().date().isoformat(); db.get_or_create_daily_state(d); \
      db.add_realised_pnl(d, -(__import__('config').MAX_DAILY_LOSS+1)); print('seeded loss')"
      python main.py --run-once   # should report kill switch tripped, no new trades
      ```
- [ ] Reset afterward:
      ```
      python -c "from src.storage import db; from src.utils import market_calendar as mc; \
      import sqlite3; \
      [c.execute('UPDATE daily_state SET realised_pnl=0, kill_switch_tripped=0') for c in [db.get_conn().__enter__()]]"
      ```
      (or just delete `data/trading_buddy.db` to start fresh — you'll lose paper history.)

---

## 5. The LIVE flip (the point of no return)

- [ ] Reduce `CAPITAL`-related risk if needed; confirm `MIN_LOT_SIZE = 1` and
      `MAX_RISK_PER_TRADE` / `MAX_DAILY_LOSS` are what you can afford to lose.
- [ ] Edit `config.py`: set `MODE = "LIVE"`.
- [ ] Make sure funds are in the Zerodha account.
- [ ] Start with the confirmation flag: `python main.py --paper-trade --confirm-live`
      on a day you can watch it, for the **first real order** — verify on Kite that the
      order placed correctly **with its stop attached**.
- [ ] Only after that single supervised trade looks right, let the scheduler run live:
      `python main.py --confirm-live`.
- [ ] To update the launcher for live use, edit `Start Trading Buddy.bat`'s last
      `python main.py` line to `python main.py --confirm-live`.

---

## Daily user's job once live

Unchanged from PAPER — they still just see Telegram alerts. **Do not tell a
non-technical daily user to manage LIVE mode.** Going live, funding, and the
`--confirm-live` flag are the operator's responsibility.

## Emergency stop

- Telegram: send **`/pause`** to halt new trades (positions/stops stay).
- Hard stop: close the running window (Ctrl-C / X). Open orders and stops remain at the
  exchange — manage them in the Kite app if needed.
