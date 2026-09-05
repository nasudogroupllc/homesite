# Automated Swing Trading System — Plain-English Guide

This is a complete, automated swing-trading system for your Ubuntu VPS. It scans
the market every evening, places protective bracket orders every morning, and
messages you on Telegram whenever something happens. It trades **paper (fake)
money** on Alpaca until you explicitly decide to go live.

**You do not need to know how to code to run this.** Everything you'll ever
change lives in one plain-text file (`config.yaml`), and every action is a
copy-paste command below.

> ⚠️ **Important honesty note:** This system was built and tested with *synthetic*
> (made-up) data because your real market-data endpoint and Alpaca account live on
> *your* VPS, not on the machine it was built on. All the logic is tested and
> working. **You must run the backtest on your own VPS to see real historical
> results before you trade** — that is Step 5 below, and it takes one command.

---

## 1. What the system does (the big picture)

There are three main programs plus a backtester:

| Program | When it runs | What it does |
|---|---|---|
| **scanner.py** | 5:00 PM ET, Mon–Fri | Looks at ~500 stocks, keeps only the ones that pass all your rules, ranks them by strength, and saves the winners to `candidates.json`. |
| **executor.py** | 9:28 AM ET, Mon–Fri | Reads the candidates, checks your risk limits, and places **bracket orders** (each order has a built-in stop-loss and profit target). Also closes positions older than 14 days and halts trading if your account is down too much. |
| **notifier.py** | Whenever needed | Sends you a Telegram message: candidates found, order filled, stop/target hit, time-stop closed a position, weekly summary, or any error. |
| **backtest.py** | Whenever you want | Replays the strategy over the last 2 years and shows total trades, win rate, average R, max drawdown, and an equity-curve chart. |

The system also automatically:
- Cancels entry orders that never filled (3:45 PM ET).
- Fixes each order's stop/target to the *real* fill price after it fills.
- **Immediately closes any position that ends up without a protective stop** and alerts you.
- Logs everything to `trades.log` and records every trade in `journal.csv`.

---

## 2. What you need before you start

1. **Your Ubuntu VPS** (you already have it).
2. **Market-data access** to the hosted thetadata.net endpoint
   (`https://marketdata.boxrun.xyz`), using the shared Bearer token you'll put in
   `.env` as `MARKETDATA_API_KEY`. Nothing to install — it's a remote service,
   and the token is the only thing required (no IP allow-listing). If the
   endpoint is ever unreachable, the system automatically falls back to Alpaca's
   data and warns you on Telegram.
3. **Alpaca paper-trading API keys** (you already have these in
   `/home/trader/alpaca-trading/.env`).
4. **Your existing Telegram bot** token and chat id.

---

## 3. One-time installation

**Step 1 — Put the files on your VPS.** Copy this whole `swing-trading` folder
into your trading directory, for example:

```
/home/trader/alpaca-trading/swing-trading
```

**Step 2 — Add the missing secrets to your `.env` file.** Open it:

```
nano /home/trader/alpaca-trading/.env
```

Make sure these lines exist (your Alpaca keys are probably already there — just
add any that are missing). See `.env.example` for the full template:

```
ALPACA_API_KEY=...your paper key...
ALPACA_SECRET_KEY=...your paper secret...
ALPACA_BASE_URL=https://paper-api.alpaca.markets
MARKETDATA_BASE_URL=https://marketdata.boxrun.xyz
MARKETDATA_API_KEY=...the shared bearer token you were given...
TELEGRAM_BOT_TOKEN=...your bot token...
TELEGRAM_CHAT_ID=...your chat id...
```

Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`.

**Step 3 — Run the installer.** This creates a private Python environment and
installs everything. It does **not** trade:

```
cd /home/trader/alpaca-trading/swing-trading
bash deploy/setup.sh
```

When it finishes, it prints your exact next commands.

---

## 4. Test that messaging works

```
./run.sh notifier.py test
```

You should get a Telegram message within a few seconds. If not, double-check
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in your `.env`.

### 4b. Test that market data works

```
./run.sh datafeed.py test AAPL
```

This confirms the hosted endpoint answers, your Bearer token is accepted, and
real daily bars come back. You should see something like `OK - got 300 daily
bars`. If it fails with an auth error, re-check that `MARKETDATA_API_KEY` in your
`.env` matches the token exactly (no extra spaces or quotes).

---

## 5. See the backtest results (do this BEFORE trading)

```
./run.sh backtest.py
```

This reads 2+ years of daily data from the market-data endpoint and prints a
summary like:

```
 Total trades:      xxx
 Win rate:          xx.x%
 Average R:         +x.xx
 Max drawdown:      xx.x%
```

It also saves a chart to `equity_curve.png` in the project folder. Download that
file to your computer to look at it (or open it in any image viewer on the VPS).

> If the market-data endpoint isn't set up yet and you just want to confirm the
> program itself works, run `./run.sh backtest.py --synthetic`. That uses fake data — the
> numbers are meaningless, it only proves the machinery runs.

**Only move on once you're comfortable with the real backtest numbers.**

---

## 6. Do one manual dry-run

Run these two by hand and read what they print (they use paper money, so it's safe):

```
./run.sh scanner.py            # builds tonight's candidate list
./run.sh executor.py morning   # would place paper orders for those candidates
```

Then check everything at a glance:

```
./run.sh status.py
```

---

## 7. Turn on the automatic schedule

When you're happy, switch on the daily automation (this uses **cron**, Ubuntu's
built-in scheduler). The installer already generated a file with the correct
times (all in New York time):

```
crontab deploy/crontab.generated
```

Check it's installed:

```
crontab -l
```

That's it — the system now runs on its own every trading day.

*(Prefer systemd timers instead of cron? Run `sudo deploy/install_systemd.sh`.
You only need one or the other, not both.)*

---

## 8. How to check on it any time

```
./run.sh status.py
```

Shows your equity, high-water mark, current drawdown, open positions, open
orders, tonight's candidates, and your most recent trades.

To watch the live log:

```
tail -f trades.log
```

---

## 9. How to read your trade journal

Open `journal.csv` in any spreadsheet program (Excel, Google Sheets, LibreOffice)
or view it on the VPS:

```
column -s, -t journal.csv | less -S
```

Each row is one trade with these columns:

- **date** – when you entered
- **symbol** – the stock
- **entry / stop / target** – your prices
- **shares** – how many you bought
- **r_risked** – dollars you risked (this is "1R")
- **exit_date / exit_price** – when and where you got out
- **r_result** – profit/loss measured in R (e.g. `+2.0` means you made twice
  what you risked; `-1.0` means you lost exactly what you risked)
- **holding_days** – how long you held
- **status** – OPEN, TARGET, STOP, or TIME_STOP

---

## 10. How to change the strategy (config.yaml)

Open the settings file:

```
nano config.yaml
```

Change any number, save (`Ctrl+O`, `Enter`), and exit (`Ctrl+X`). The next run
uses the new value — **you never edit the code.** Common ones:

| Setting | Default | Meaning |
|---|---|---|
| `account_risk_pct` | 1.0 | % of your account risked per trade |
| `atr_stop_multiple` | 1.5 | how wide the stop-loss is |
| `reward_multiple` | 2.0 | profit target size (2R) |
| `max_positions` | 4 | most trades open at once |
| `max_sector_positions` | 1 | most trades in one sector |
| `max_position_size_pct` | 30 | biggest single position (% of account) |
| `drawdown_halt_pct` | 8 | stop new trades if account is down this % |
| `time_stop_days` | 30 | force-close trades after this many **trading days** held (0 = off) |
| `price_range` | 15–400 | `price_min` / `price_max` |
| `min_dollar_volume` | 50,000,000 | minimum daily dollars traded |

---

## 11. Editing which stocks are watched (universe.csv)

`universe.csv` is a simple two-column list: `symbol,sector`. Open it in a
spreadsheet or text editor to add/remove stocks. The **sector matters** — the
system won't hold more than one position per sector, so every symbol needs a
correct sector label.

To rebuild the full S&P 500 + Nasdaq 100 list automatically:

```
./run.sh build_universe.py
```

---

## 12. Start / stop / pause

- **Pause everything (stop the schedule):** `crontab -r`
  (or, if you used systemd: `sudo systemctl disable --now 'swing-*.timer'`)
- **Resume:** `crontab deploy/crontab.generated`
- **Close a position right now:** do it manually in the Alpaca dashboard, or let
  the stop/target/time-stop handle it.
- **Stop *new* trades but keep managing open ones:** set `max_positions: 0` in
  `config.yaml`.

---

## 13. Going live (only when YOU decide)

The system is locked to **paper trading**. To switch to real money later, change
one line in your `.env`:

```
ALPACA_BASE_URL=https://api.alpaca.markets
```

and use your **live** Alpaca keys. `status.py` will clearly show `LIVE MONEY`
when this is active. Do this only after you've watched paper trading behave the
way you expect for a good while.

---

## 14. Troubleshooting

- **No Telegram messages:** check `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, then
  run `./run.sh notifier.py test`.
- **"Could not reach Alpaca":** check your keys and that `ALPACA_BASE_URL` is the
  paper URL.
- **"ThetaData was unreachable" / fallback warning:** the market-data endpoint
  (`marketdata.boxrun.xyz`) couldn't be reached, so the system used Alpaca data
  instead — trading continues. Run `./run.sh datafeed.py test AAPL` to see the
  exact problem (network vs. bad token vs. IP not allowed).
- **Nothing traded this morning:** open `trades.log` — it says exactly why each
  candidate was skipped (sector full, position too big, drawdown halt, etc.).
- **See errors:** every crash is also sent to you on Telegram.

---

## 15. What's realistic to expect (honest limitations)

- The **backtest doesn't include the earnings blackout** (free historical
  earnings dates are unreliable). Live trading *does* skip earnings. So real
  results tend to be a touch better than the backtest on that front.
- The backtest doesn't add slippage or fees (Alpaca is commission-free, but real
  fills can differ by pennies).
- A backtest is a guide, not a promise. Markets change. Start on paper, watch it,
  and only risk money you can afford to lose. This is software, not financial
  advice.
