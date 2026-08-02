"""Solana Ecosystem Auto-Updating Report & Dashboard

Collects ecosystem metrics from public, keyless sources:
  - Solana RPC (api.mainnet-beta.solana.com): health, slot, block time,
    epoch info, performance samples, vote accounts, supply
  - DeFiLlama: Solana TVL + 24h DEX volume + chain history
  - CoinGecko: SOL price, market cap, 24h change, 14-day price history

Outputs:
  - report.json  : structured machine-readable data
  - report.md    : human-readable report
  - report.html  : self-contained dark-theme interactive dashboard

Usage:
  python pipeline.py            # fetch once, write outputs
  python pipeline.py --loop N   # re-fetch every N seconds (automation mode)

No API keys required. Python 3.10+.
"""
import json
import math
import sys
import time
import urllib.request
from datetime import datetime, timezone

RPC = "https://api.mainnet-beta.solana.com"
DEFILLAMA = "https://api.llama.fi"
COINGECKO = "https://api.coingecko.com/api/v3"
UA = {"User-Agent": "solana-ecosystem-dashboard/1.0"}


# ---------------------------------------------------------------- HTTP helpers
def http_json(url, data=None, timeout=25):
    headers = dict(UA)
    if data is not None:
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def rpc(method, params=None):
    res = http_json(RPC, data={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or []
    })
    if "error" in res:
        raise RuntimeError(f"RPC {method}: {res['error']}")
    return res.get("result")


# ------------------------------------------------------------------ collectors
def collect_rpc():
    out = {}
    out["health"] = rpc("getHealth")
    epoch = rpc("getEpochInfo")
    out["epoch"] = epoch
    out["epoch_progress_pct"] = round(100 * epoch["slotIndex"] / epoch["slotsInEpoch"], 2)

    samples = rpc("getRecentPerformanceSamples", [60])
    out["perf_samples"] = samples
    # Aggregate recent throughput across samples
    total_tx = sum(s.get("numTransactions", 0) for s in samples)
    total_secs = sum(s.get("samplePeriodSecs", 0) for s in samples)
    total_slots = sum(s.get("numSlots", 0) for s in samples)
    out["tps_avg"] = round(total_tx / total_secs, 1) if total_secs else None
    out["slot_time_sec"] = round(total_secs / total_slots, 3) if total_slots else None

    vote = rpc("getVoteAccounts")
    cur = vote.get("current", [])
    dlq = vote.get("delinquent", [])
    out["validators"] = {
        "active": len(cur),
        "delinquent": len(dlq),
        "delinquency_pct": round(100 * len(dlq) / max(1, len(cur) + len(dlq)), 2),
        "top_by_stake": sorted(
            (
                {
                    "node": v.get("nodePubkey", "")[:12] + "...",
                    "vote": v.get("votePubkey", "")[:12] + "...",
                    "stake_sol": round(v.get("activatedStake", 0) / 1e9),
                    "commission_pct": v.get("commission"),
                }
                for v in cur
            ),
            key=lambda x: x["stake_sol"],
            reverse=True,
        )[:10],
        "commission_avg_pct": (
            round(sum(v.get("commission", 0) for v in cur) / len(cur), 2) if cur else None
        ),
    }

    supply = rpc("getSupply")
    out["supply"] = {
        "total_sol": round(supply["value"]["total"] / 1e9),
        "circulating_sol": round(supply["value"]["circulating"] / 1e9),
    }
    return out


def collect_defillama():
    out = {}
    hist = http_json(f"{DEFILLAMA}/v2/historicalChainTvl/Solana")
    out["tvl_usd"] = hist[-1]["tvl"] if hist else None
    out["tvl_history"] = [
        {"date": datetime.fromtimestamp(p["date"], tz=timezone.utc).strftime("%Y-%m-%d"),
         "tvl": round(p["tvl"])}
        for p in hist[-90:]
    ]
    if len(hist) >= 2:
        prev = hist[-2]["tvl"]
        out["tvl_change_24h_pct"] = round(
            100 * (out["tvl_usd"] - prev) / prev, 2) if prev else None

    # DEX volume (Solana) — 24h, 7d and recent daily chart
    vol = http_json(
        f"{DEFILLAMA}/overview/dexs/solana?excludeTotalDataChart=false"
        "&excludeTotalDataChartBreakdown=true&dataType=dailyVolume")
    out["dex_volume_24h_usd"] = vol.get("total24h")
    chart = vol.get("totalDataChart", [])
    out["dex_volume_7d_usd"] = [
        {"date": datetime.fromtimestamp(p[0], tz=timezone.utc).strftime("%Y-%m-%d"),
         "volume": round(p[1])}
        for p in chart[-7:]
    ] if chart else []
    return out


def collect_coingecko():
    price = http_json(
        f"{COINGECKO}/simple/price?ids=solana&vs_currencies=usd"
        "&include_market_cap=true&include_24hr_change=true&include_24hr_vol=true")
    s = price.get("solana", {})
    out = {
        "price_usd": s.get("usd"),
        "market_cap_usd": s.get("usd_market_cap"),
        "change_24h_pct": round(s.get("usd_24h_change", 0), 2),
        "volume_24h_usd": s.get("usd_24h_vol"),
    }
    mc = http_json(f"{COINGECKO}/coins/solana/market_chart?vs_currency=usd&days=14&interval=daily")
    out["price_history_14d"] = [
        {"date": datetime.fromtimestamp(p[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
         "price": round(p[1], 2)}
        for p in mc.get("prices", [])
    ]
    return out


# ------------------------------------------------------------------ anomalies
def detect_anomalies(data):
    alerts = []
    tps = data["rpc"].get("tps_avg")
    if tps is not None:
        if tps < 800:
            alerts.append(("HIGH", f"TPS {tps} unusually low (<800)"))
        elif tps > 5000:
            alerts.append(("HIGH", f"TPS {tps} unusually high (>5000)"))
    slot_time = data["rpc"].get("slot_time_sec")
    if slot_time is not None and slot_time > 0.6:
        alerts.append(("HIGH", f"Slow slot time {slot_time}s (>0.6s)"))
    delq = data["rpc"]["validators"]["delinquency_pct"]
    if delq > 1.0:
        alerts.append(("MEDIUM", f"Validator delinquency {delq}% (>1%)"))
    tvl_chg = data["defillama"].get("tvl_change_24h_pct")
    if tvl_chg is not None and abs(tvl_chg) > 5:
        alerts.append(("MEDIUM", f"TVL moved {tvl_chg}% in 24h"))
    px_chg = data["coingecko"].get("change_24h_pct")
    if px_chg is not None and abs(px_chg) > 7:
        alerts.append(("MEDIUM", f"SOL price moved {px_chg}% in 24h"))
    if data["rpc"].get("health") != "ok":
        alerts.append(("HIGH", f"RPC health check returned: {data['rpc'].get('health')}"))
    return alerts


# ------------------------------------------------------------------ assemble
def build_report():
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rpc": collect_rpc(),
        "defillama": collect_defillama(),
        "coingecko": collect_coingecko(),
    }
    data["alerts"] = detect_anomalies(data)
    return data


# ------------------------------------------------------------------ outputs
def write_json(data, path="report.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def fmt_usd(v):
    if v is None:
        return "n/a"
    if v >= 1e12:
        return f"${v/1e12:.2f}T"
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.2f}M"
    return f"${v:,.2f}"


def write_markdown(data, path="report.md"):
    r = data["rpc"]
    d = data["defillama"]
    c = data["coingecko"]
    lines = [
        "# Solana Ecosystem Report",
        f"*Auto-generated {data['generated_at']}*",
        "",
        "## Market",
        f"- **SOL price:** {fmt_usd(c['price_usd'])} ({c['change_24h_pct']}% 24h)",
        f"- **Market cap:** {fmt_usd(c['market_cap_usd'])}",
        f"- **24h volume:** {fmt_usd(c['volume_24h_usd'])}",
        "",
        "## DeFi",
        f"- **Solana TVL:** {fmt_usd(d['tvl_usd'])} ({d.get('tvl_change_24h_pct')}% 24h)",
        f"- **DEX volume (24h):** {fmt_usd(d.get('dex_volume_24h_usd'))}",
        "",
        "## Network",
        f"- **Health:** {r['health']}",
        f"- **Avg TPS (recent samples):** {r.get('tps_avg')}",
        f"- **Avg slot time:** {r.get('slot_time_sec')}s",
        f"- **Epoch:** {r['epoch']['epoch']} ({r['epoch_progress_pct']}% complete)",
        f"- **Absolute slot:** {r['epoch']['absoluteSlot']:,}",
        "",
        "## Validators",
        f"- **Active:** {r['validators']['active']}  |  **Delinquent:** {r['validators']['delinquent']}"
        f" ({r['validators']['delinquency_pct']}%)",
        f"- **Avg commission:** {r['validators']['commission_avg_pct']}%",
        "",
        "### Top validators by stake",
        "| # | Node | Stake (SOL) | Commission |",
        "|---|------|------------|------------|",
    ]
    for i, v in enumerate(r["validators"]["top_by_stake"], 1):
        lines.append(f"| {i} | {v['node']} | {v['stake_sol']:,} | {v['commission_pct']}% |")
    if data["alerts"]:
        lines += ["", "## Anomaly alerts"]
        for sev, msg in data["alerts"]:
            lines.append(f"- **[{sev}]** {msg}")
    lines += [
        "",
        "## Supply",
        f"- **Total SOL:** {r['supply']['total_sol']:,}",
        f"- **Circulating SOL:** {r['supply']['circulating_sol']:,}",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def svg_line(points, w=520, h=140, stroke="#22d3ee"):
    """Minimal inline SVG sparkline from a list of (label, value) points."""
    if len(points) < 2:
        return ""
    vals = [p[1] for p in points]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    step = w / (len(points) - 1)
    pts = " ".join(
        f"{i*step:.1f},{h - ((v - lo) / span) * (h - 10) - 5:.1f}"
        for i, v in enumerate(vals))
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline fill="none" stroke="{stroke}" stroke-width="2" points="{pts}"/>'
            f"</svg>")


def write_html(data, path="report.html"):
    r, d, c = data["rpc"], data["defillama"], data["coingecko"]
    price_chart = svg_line([(p["date"], p["price"]) for p in c.get("price_history_14d", [])],
                           stroke="#a3e635")
    tvl_chart = svg_line([(p["date"], p["tvl"]) for p in d.get("tvl_history", [])])
    vol_chart = svg_line([(p["date"], p["volume"]) for p in d.get("dex_volume_7d_usd", [])],
                         stroke="#f472b6")
    epoch = r["epoch"]
    epoch_pct = r["epoch_progress_pct"]

    def card(title, value, sub=""):
        return (f'<div class="card"><div class="t">{title}</div>'
                f'<div class="v">{value}</div><div class="s">{sub}</div></div>')

    top_rows = "".join(
        f"<tr><td>{i}</td><td><code>{v['node']}</code></td>"
        f"<td>{v['stake_sol']:,}</td><td>{v['commission_pct']}%</td></tr>"
        for i, v in enumerate(r["validators"]["top_by_stake"], 1))

    alerts_html = ""
    if data["alerts"]:
        items = "".join(
            f'<li class="alert {sev.lower()}"><b>[{sev}]</b> {msg}</li>'
            for sev, msg in data["alerts"])
        alerts_html = f'<div class="panel"><h2>Anomaly alerts</h2><ul>{items}</ul></div>'
    else:
        alerts_html = '<div class="panel"><h2>Anomaly alerts</h2><p class="ok">All monitored metrics within normal bands.</p></div>'

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Solana Ecosystem Dashboard</title>
<style>
  :root{{--bg:#0b0f17;--panel:#111827;--muted:#6b7280;--text:#e5e7eb;--accent:#22d3ee;--good:#a3e635;--warn:#f59e0b;--bad:#ef4444}}
  *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font-family:ui-sans-serif,system-ui,Segoe UI,Roboto,Arial}}
  .wrap{{max-width:1080px;margin:0 auto;padding:28px 18px 60px}}
  h1{{font-size:22px;margin:0 0 4px}} .sub{{color:var(--muted);font-size:13px;margin-bottom:22px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;margin-bottom:22px}}
  .card{{background:var(--panel);border:1px solid #1f2937;border-radius:14px;padding:16px}}
  .card .t{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}}
  .card .v{{font-size:26px;font-weight:700;margin:6px 0 2px}} .card .s{{color:var(--muted);font-size:12px}}
  .panel{{background:var(--panel);border:1px solid #1f2937;border-radius:14px;padding:18px;margin-bottom:18px}}
  .panel h2{{margin:0 0 12px;font-size:15px}} .charts{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
  .chart{{background:var(--panel);border:1px solid #1f2937;border-radius:14px;padding:14px}}
  .chart h3{{margin:0 0 8px;font-size:13px;color:var(--muted);font-weight:600}}
  table{{width:100%;border-collapse:collapse;font-size:13px}} td,th{{padding:8px 10px;border-bottom:1px solid #1f2937;text-align:left}}
  th{{color:var(--muted);font-weight:600;text-transform:uppercase;font-size:11px;letter-spacing:.05em}}
  .bar{{background:#1f2937;border-radius:99px;height:10px;overflow:hidden;margin-top:6px}}
  .bar>div{{height:100%;background:linear-gradient(90deg,var(--accent),var(--good));width:{epoch_pct}%}}
  .alert{{list-style:none;padding:10px 12px;border-radius:10px;margin:8px 0;border-left:3px solid}}
  .alert.high{{background:rgba(239,68,68,.08);border-color:var(--bad)}}
  .alert.medium{{background:rgba(245,158,11,.08);border-color:var(--warn)}}
  .ok{{color:var(--good)}} code{{color:var(--accent);font-size:12px}}
  footer{{color:var(--muted);font-size:12px;margin-top:10px}}
  @media(max-width:720px){{.charts{{grid-template-columns:1fr}}}}
</style></head><body><div class="wrap">
<h1>Solana Ecosystem Dashboard</h1>
<div class="sub">Auto-generated {data['generated_at']} &middot; sources: Solana RPC, DeFiLlama, CoinGecko (no API keys)</div>

<div class="grid">
  {card("SOL price", fmt_usd(c['price_usd']), f"{c['change_24h_pct']}% 24h")}
  {card("Market cap", fmt_usd(c['market_cap_usd']), "")}
  {card("Solana TVL", fmt_usd(d['tvl_usd']), f"{d.get('tvl_change_24h_pct')}% 24h")}
  {card("DEX volume 24h", fmt_usd(d.get('dex_volume_24h_usd')), "")}
  {card("Avg TPS", r.get('tps_avg') or 'n/a', "recent performance samples")}
  {card("Slot time", f"{r.get('slot_time_sec')}s", "target ~0.4s")}
  {card("Validators", f"{r['validators']['active']} active", f"{r['validators']['delinquent']} delinquent ({r['validators']['delinquency_pct']}%)")}
  {card("Health", r['health'], "")}
</div>

{alerts_html}

<div class="panel"><h2>Epoch {epoch['epoch']} progress</h2>
  <div style="display:flex;justify-content:space-between;font-size:13px;color:var(--muted)">
    <span>Slot {epoch['slotIndex']:,} / {epoch['slotsInEpoch']:,}</span><span>{epoch_pct}%</span></div>
  <div class="bar"><div></div></div></div>

<div class="charts">
  <div class="chart"><h3>SOL price — last 14 days</h3>{price_chart}</div>
  <div class="chart"><h3>Solana TVL — last 90 days</h3>{tvl_chart}</div>
</div>
<div class="charts" style="margin-top:14px">
  <div class="chart"><h3>DEX volume — last 7 days</h3>{vol_chart}</div>
  <div class="panel" style="margin:0"><h2>Top validators by stake</h2>
    <table><tr><th>#</th><th>Node</th><th>Stake (SOL)</th><th>Commission</th></tr>{top_rows}</table></div>
</div>

<div class="panel" style="margin-top:14px"><h2>Supply</h2>
  <p>Total SOL: <b>{r['supply']['total_sol']:,}</b> &nbsp;&middot;&nbsp; Circulating: <b>{r['supply']['circulating_sol']:,}</b></p></div>

<footer>Generated by the Solana Ecosystem Dashboard pipeline. Refresh by re-running <code>python pipeline.py</code>.</footer>
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def run_once():
    data = build_report()
    write_json(data)
    write_markdown(data)
    write_html(data)
    return data


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--loop":
        interval = int(args[1]) if len(args) > 1 else 900
        print(f"Automation mode: refreshing every {interval}s. Ctrl+C to stop.")
        while True:
            try:
                d = run_once()
                print(f"[{d['generated_at']}] report refreshed "
                      f"(TPS={d['rpc'].get('tps_avg')}, alerts={len(d['alerts'])})")
            except Exception as e:
                print(f"refresh failed: {e}")
            time.sleep(interval)
    else:
        d = run_once()
        print(f"Wrote report.json, report.md, report.html  "
              f"(TPS={d['rpc'].get('tps_avg')}, TVL={fmt_usd(d['defillama']['tvl_usd'])}, "
              f"alerts={len(d['alerts'])})")
