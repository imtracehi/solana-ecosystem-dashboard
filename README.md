# Solana Ecosystem Auto-Updating Report & Interactive Dashboard

A keyless, automatically-updating snapshot of the Solana ecosystem: network
health, validator stats, market, and DeFi metrics — refreshed on a schedule
and rendered in three formats.

## How it works

`pipeline.py` collects data from three public sources (no API keys):

| Source | Data | Auth |
|--------|------|------|
| Solana RPC (`api.mainnet-beta.solana.com`) | health, slot/block time, epoch progress, performance samples (TPS, slot time), vote accounts (active/delinquent, stake, commissions), supply | none |
| DeFiLlama | Solana TVL (90-day history), DEX volume (24h/7d) | none |
| CoinGecko | SOL price, market cap, 24h change/volume, 14-day price history | none |

Outputs written on every run:

- **`report.html`** — self-contained dark-theme interactive dashboard (pure
  inline SVG, zero external dependencies, works offline)
- **`report.md`** — human-readable report
- **`report.json`** — structured machine-readable data

### Anomaly detection
Each run checks monitored metrics against threshold bands and flags them:

- TPS unusually low (<800) or high (>5000)
- Slot time slower than 0.6s (target ~0.4s)
- Validator delinquency above 1%
- TVL or SOL price moves beyond ±5% / ±7% in 24h
- RPC health not `ok`

Alerts render in the HTML dashboard and the Markdown report.

## Automation

Three refresh modes:

1. **Local, on demand** — `python pipeline.py`
2. **Local, looping** — `python pipeline.py --loop 900` (re-fetch every 15 min)
3. **Local, scheduled** — `run_daily.ps1` (daily; register with Windows
   Task Scheduler)
4. **Cloud, scheduled** — GitHub Actions workflow (`.github/workflows/refresh.yml`)
   runs the pipeline daily and deploys the dashboard to GitHub Pages, so the
   live report stays current with zero maintenance.

## Requirements

- Python 3.10+ (standard library only — no third-party packages)
- Network access to the three public APIs above

## Project layout

```
pipeline.py            # collector + report generator
report.html            # interactive dashboard (generated)
report.md              # human-readable report (generated)
report.json            # structured data (generated)
run_daily.ps1          # Windows scheduled-refresh helper
.github/workflows/     # GitHub Actions daily refresh + Pages deploy
```

## Live example
The dashboard is deployed to GitHub Pages from this repo and auto-refreshes
daily via the Actions workflow.
