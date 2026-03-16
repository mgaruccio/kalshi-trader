"""Tiny web dashboard for trader portfolio monitoring.

Serves a live-updating dashboard that reads from the portfolio snapshot JSON.
No dependencies beyond the stdlib.

Usage:
    uv run python scripts/portfolio_dashboard.py [--port 8080]
    # Then open http://localhost:8080 (or http://<droplet-ip>:8080)
"""
from __future__ import annotations

import argparse
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

SNAPSHOT_PATH = "/tmp/kalshi-trader-portfolio.json"

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Kalshi Trader — Portfolio</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #e6edf3;
          --dim: #8b949e; --green: #3fb950; --red: #f85149; --blue: #58a6ff; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
         background: var(--bg); color: var(--text); padding: 20px; }
  h1 { font-size: 18px; color: var(--blue); margin-bottom: 4px; }
  .meta { color: var(--dim); font-size: 12px; margin-bottom: 20px; }
  .cards { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--border);
          border-radius: 8px; padding: 16px; min-width: 180px; flex: 1; }
  .card .label { font-size: 11px; color: var(--dim); text-transform: uppercase;
                 letter-spacing: 1px; margin-bottom: 4px; }
  .card .value { font-size: 24px; font-weight: bold; }
  .pos { color: var(--green); } .neg { color: var(--red); }
  table { width: 100%; border-collapse: collapse; background: var(--card);
          border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  th { font-size: 11px; color: var(--dim); text-transform: uppercase;
       letter-spacing: 1px; padding: 10px 12px; text-align: left;
       border-bottom: 1px solid var(--border); }
  td { padding: 8px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  .r { text-align: right; }
  .error { color: var(--red); padding: 40px; text-align: center; }
  #status { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
            background: var(--green); margin-right: 6px; }
  #status.stale { background: var(--red); }
</style>
</head>
<body>
<h1>Kalshi WeatherMaker — Portfolio Dashboard</h1>
<div class="meta"><span id="status"></span>Updated <span id="ts">—</span> | Refreshes every 10s</div>
<div class="cards">
  <div class="card"><div class="label">Portfolio Value</div><div class="value" id="portfolio-value">—</div></div>
  <div class="card"><div class="label">Cash (free)</div><div class="value" id="cash">—</div></div>
  <div class="card"><div class="label">Open Contracts</div><div class="value" id="contracts">—</div></div>
  <div class="card"><div class="label">Unrealized P&L</div><div class="value" id="pnl">—</div></div>
</div>
<table>
  <thead><tr>
    <th>Instrument</th><th>Side</th><th class="r">Qty</th><th class="r">Avg Entry</th>
    <th class="r">Bid</th><th class="r">Ask</th><th class="r">MTM Value</th><th class="r">Unrl P&L</th>
  </tr></thead>
  <tbody id="positions"><tr><td colspan="8" class="error">Loading...</td></tr></tbody>
</table>
<script>
function $(id) { return document.getElementById(id); }
function fmt(cents) { return '$' + (cents / 100).toFixed(2); }
function fmtPnl(cents) {
  const s = (cents >= 0 ? '+' : '') + (cents / 100).toFixed(2);
  return '<span class="' + (cents >= 0 ? 'pos' : 'neg') + '">$' + s + '</span>';
}

async function refresh() {
  try {
    const r = await fetch('/api/portfolio?' + Date.now());
    if (!r.ok) { $('positions').innerHTML = '<tr><td colspan="8" class="error">No snapshot yet</td></tr>'; return; }
    const d = await r.json();
    const s = d.summary, a = d.account;

    $('ts').textContent = new Date(d.timestamp).toLocaleString();
    $('portfolio-value').textContent = fmt(s.portfolio_value_cents);
    $('cash').textContent = '$' + a.free_usd.toFixed(2);
    $('contracts').textContent = s.total_contracts;
    $('pnl').innerHTML = fmtPnl(s.total_unrealized_pnl_cents);

    // Stale check (>120s old)
    const age = (Date.now() - new Date(d.timestamp).getTime()) / 1000;
    $('status').className = age > 120 ? 'stale' : '';

    if (!d.positions.length) {
      $('positions').innerHTML = '<tr><td colspan="8" style="color:var(--dim)">No open positions</td></tr>';
      return;
    }

    $('positions').innerHTML = d.positions.map(p => {
      const inst = p.instrument.replace('.KALSHI', '');
      const bid = p.bid_cents != null ? p.bid_cents + '¢' : '—';
      const ask = p.ask_cents != null ? p.ask_cents + '¢' : '—';
      const mtm = p.mtm_value_cents != null ? fmt(p.mtm_value_cents) : '—';
      const pnl = p.unrealized_pnl_cents != null ? fmtPnl(p.unrealized_pnl_cents) : '—';
      return '<tr><td>' + inst + '</td><td>' + p.side + '</td>'
        + '<td class="r">' + p.qty + '</td><td class="r">' + p.avg_entry_cents + '¢</td>'
        + '<td class="r">' + bid + '</td><td class="r">' + ask + '</td>'
        + '<td class="r">' + mtm + '</td><td class="r">' + pnl + '</td></tr>';
    }).join('');
  } catch(e) { console.error(e); }
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/portfolio"):
            try:
                with open(SNAPSHOT_PATH) as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data.encode())
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())

    def log_message(self, format, *args):
        pass  # Suppress request logging


def main():
    parser = argparse.ArgumentParser(description="Portfolio dashboard web server")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = HTTPServer(("0.0.0.0", args.port), Handler)
    print(f"Dashboard running at http://0.0.0.0:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")


if __name__ == "__main__":
    main()
