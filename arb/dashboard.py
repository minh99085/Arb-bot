"""Web dashboard — PnL summary + last 50 trades (click to expand)."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from arb.config import ArbConfig
from arb.state import OpportunityStore

DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"


def _worker_status(config: ArbConfig) -> dict[str, Any] | None:
    path = config.state_root / "worker_status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def build_dashboard_payload(config: ArbConfig, *, trade_limit: int = 50) -> dict[str, Any]:
    store = OpportunityStore(config.state_db)
    summary = store.trade_summary()
    trades = store.list_trades(limit=trade_limit)
    self_tune = None
    try:
        from arb.self_tune import status_dict

        self_tune = status_dict(config)
    except Exception:
        self_tune = None
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "study_mode": config.study_mode,
        "exec_mode": config.exec_mode.value,
        "paper_realistic": config.paper_realistic,
        "paper_gamma_fallback": config.paper_gamma_fallback,
        "thresholds": {
            "min_edge_bps": config.effective_min_edge_bps(),
            "verify_top_n": config.verify_top_n,
            "max_position_usd": config.max_position_usd,
            "max_open_positions": config.max_open_positions,
            "max_daily_trades": config.max_daily_trades,
            "min_book_depth": config.min_book_depth,
            "ws_watch_sec": config.ws_watch_sec,
        },
        "summary": summary,
        "trades": trades,
        "worker": _worker_status(config),
        "self_tune": self_tune,
        "state_db": str(config.state_db),
    }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Polymarket Arb Dashboard</title>
  <style>
    :root {
      --bg: #0f1419;
      --card: #1a2332;
      --border: #2d3a4f;
      --text: #e7ecf3;
      --muted: #8b9cb3;
      --green: #3dd68c;
      --red: #f07178;
      --blue: #59c2ff;
      --amber: #ffcc66;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }
    .wrap { max-width: 1200px; margin: 0 auto; padding: 1.25rem 1.5rem 3rem; }
    h1 { font-size: 1.35rem; font-weight: 600; margin: 0 0 0.25rem; }
    .sub { color: var(--muted); font-size: 0.85rem; margin-bottom: 1.25rem; }
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 0.75rem;
      margin-bottom: 1.5rem;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.9rem 1rem;
    }
    .card .label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
    .card .value { font-size: 1.35rem; font-weight: 600; margin-top: 0.2rem; }
    .pos { color: var(--green); }
    .neg { color: var(--red); }
    .section-title {
      font-size: 0.95rem;
      font-weight: 600;
      margin: 0 0 0.75rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .badge {
      font-size: 0.7rem;
      background: var(--border);
      padding: 0.15rem 0.45rem;
      border-radius: 999px;
      color: var(--muted);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.82rem;
    }
    thead th {
      text-align: left;
      color: var(--muted);
      font-weight: 500;
      padding: 0.55rem 0.65rem;
      border-bottom: 1px solid var(--border);
    }
    tbody tr.trade-row {
      cursor: pointer;
      border-bottom: 1px solid var(--border);
    }
    tbody tr.trade-row:hover { background: #1f2a3d; }
    tbody tr.trade-row.open { background: #1c2838; }
    tbody td { padding: 0.6rem 0.65rem; vertical-align: top; }
    .detail-row { display: none; background: #141c28; }
    .detail-row.open { display: table-row; }
    .detail-cell { padding: 0.75rem 1rem 1rem !important; }
    .detail-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 0.75rem 1.25rem;
      font-size: 0.8rem;
    }
    .detail-grid dt { color: var(--muted); margin: 0; }
    .detail-grid dd { margin: 0.1rem 0 0.5rem; word-break: break-word; }
    .mode-pill {
      display: inline-block;
      padding: 0.1rem 0.45rem;
      border-radius: 4px;
      font-size: 0.72rem;
      background: #243044;
      color: var(--blue);
    }
    .empty {
      text-align: center;
      color: var(--muted);
      padding: 2.5rem 1rem;
      border: 1px dashed var(--border);
      border-radius: 10px;
    }
    .refresh { color: var(--muted); font-size: 0.75rem; }
    .banner {
      background: #1e3a5f;
      border: 1px solid #2d5a8a;
      border-radius: 10px;
      padding: 0.75rem 1rem;
      margin-bottom: 1rem;
      font-size: 0.85rem;
      color: #b8d4f0;
    }
    .banner strong { color: var(--blue); }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Polymarket Arb Dashboard</h1>
    <div class="banner" id="banner"></div>
    <p class="sub">Live Polymarket data &middot; realistic paper fills &middot; auto-refresh 30s &middot; click a row to expand</p>
    <div class="cards" id="cards"></div>
    <div class="section-title">Live thresholds <span class="badge" id="tune-badge">self-tune</span></div>
    <div class="cards" id="thresh"></div>
    <div class="section-title">Trade history <span class="badge" id="trade-count">last 50</span></div>
    <div id="table-wrap"></div>
    <p class="refresh" id="meta"></p>
  </div>
  <script>
    const fmtUsd = (n) => {
      if (n == null || Number.isNaN(n)) return "-";
      const s = Number(n).toFixed(4);
      return (n >= 0 ? "$" : "-$") + Math.abs(Number(n)).toFixed(4);
    };
    const fmtPct = (bps) => (bps == null ? "-" : Number(bps).toFixed(1) + " bps");
    const pnlClass = (n) => (n == null ? "" : (n >= 0 ? "pos" : "neg"));

    function renderBanner(data) {
      const el = document.getElementById("banner");
      if (data.paper_realistic) {
        el.innerHTML = "<strong>Realistic paper mode</strong> — scans live Gamma + CLOB, trades only " +
          "CLOB-verified arbs, re-checks order books at execution. Simulated fills (not real money). " +
          "Min edge: " + (data.thresholds && data.thresholds.min_edge_bps) + " bps.";
      } else if (data.exec_mode === "live") {
        el.innerHTML = "<strong>Live mode</strong> — real orders with real money.";
      } else {
        el.innerHTML = "<strong>Exploration paper mode</strong> — relaxed thresholds; PnL may overstate live results.";
      }
    }

    function renderCards(data) {
      renderBanner(data);
      const s = data.summary || {};
      const cards = [
        ["Realized PnL", fmtUsd(s.realized_pnl_sum), pnlClass(s.realized_pnl_sum)],
        ["Today PnL", fmtUsd(s.realized_pnl_today), pnlClass(s.realized_pnl_today)],
        ["Expected PnL", fmtUsd(s.expected_pnl_sum), ""],
        ["Total trades", String(s.fill_count || 0), ""],
        ["W / L", (s.wins || 0) + " / " + (s.losses || 0), ""],
        ["Open positions", String(s.open_positions || 0), ""],
      ];
      document.getElementById("cards").innerHTML = cards.map(([label, val, cls]) =>
        `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${val}</div></div>`
      ).join("");

      const t = data.thresholds || {};
      const st = data.self_tune || {};
      document.getElementById("tune-badge").textContent =
        st.enabled === false ? "self-tune off" : "self-tune on";
      const thresh = [
        ["Min edge", (t.min_edge_bps != null ? t.min_edge_bps + " bps" : "-"), ""],
        ["Verify top N", String(t.verify_top_n ?? "-"), ""],
        ["Max size", fmtUsd(t.max_position_usd), ""],
        ["Max open", String(t.max_open_positions ?? "-"), ""],
        ["Daily trades", String(t.max_daily_trades ?? "-"), ""],
        ["Book depth", String(t.min_book_depth ?? "-"), ""],
      ];
      document.getElementById("thresh").innerHTML = thresh.map(([label, val, cls]) =>
        `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${val}</div></div>`
      ).join("");
    }

    function renderTable(data) {
      const trades = data.trades || [];
      const wrap = document.getElementById("table-wrap");
      document.getElementById("trade-count").textContent = "last " + trades.length;
      if (!trades.length) {
        wrap.innerHTML = '<div class="empty">No trades yet. Start the worker with START.ps1 and wait for paper fills.</div>';
        return;
      }
      let html = '<table><thead><tr>' +
        '<th></th><th>Time (UTC)</th><th>Mode</th><th>Market</th><th>Kind</th>' +
        '<th>Edge</th><th>Size</th><th>Expected</th><th>Realized</th><th>State</th>' +
        '</tr></thead><tbody>';
      trades.forEach((t, i) => {
        const rid = "row-" + i;
        const did = "detail-" + i;
        const tshort = (t.filled_at || "").replace("T", " ").slice(0, 19);
        const q = (t.question || "?").slice(0, 55);
        html += `<tr class="trade-row" data-detail="${did}" onclick="toggleDetail('${did}', this)">` +
          `<td class="chevron">&#9654;</td>` +
          `<td>${tshort}</td>` +
          `<td><span class="mode-pill">${t.mode || "?"}</span></td>` +
          `<td title="${(t.question||"").replace(/"/g,"")}">${q}</td>` +
          `<td>${t.kind || "-"}</td>` +
          `<td>${fmtPct(t.edge_bps)}</td>` +
          `<td>${fmtUsd(t.size_usd)}</td>` +
          `<td class="${pnlClass(t.expected_pnl)}">${fmtUsd(t.expected_pnl)}</td>` +
          `<td class="${pnlClass(t.realized_pnl)}">${fmtUsd(t.realized_pnl)}</td>` +
          `<td>${t.state || "-"}</td>` +
          `</tr>`;
        const prices = (t.fill_prices_list || []).map((p, j) =>
          (t.outcomes && t.outcomes[j] ? t.outcomes[j] + ": " : "") + p
        ).join(", ") || "-";
        html += `<tr class="detail-row" id="${did}"><td colspan="10" class="detail-cell">` +
          `<dl class="detail-grid">` +
          `<div><dt>Question</dt><dd>${t.question || "-"}</dd></div>` +
          `<div><dt>Slug</dt><dd>${t.slug || "-"}</dd></div>` +
          `<div><dt>Condition ID</dt><dd>${t.condition_id || "-"}</dd></div>` +
          `<div><dt>Source</dt><dd>${t.source || "-"} (CLOB at execution)</dd></div>` +
          `<div><dt>Fill total</dt><dd>${t.fill_total != null ? t.fill_total : "-"}</dd></div>` +
          `<div><dt>Fees / Slippage</dt><dd>${fmtUsd(t.fees_usd)} / ${fmtUsd(t.slippage_usd)}</dd></div>` +
          `<div><dt>Fill prices</dt><dd>${prices}</dd></div>` +
          `<div><dt>Ask / Bid depth</dt><dd>${t.ask_depth ?? "-"} / ${t.bid_depth ?? "-"}</dd></div>` +
          `<div><dt>Detected</dt><dd>${(t.detected_at||"").replace("T"," ").slice(0,19)}</dd></div>` +
          `<div><dt>Fill ID</dt><dd>${t.fill_id} (opp ${t.opportunity_id})</dd></div>` +
          `</dl></td></tr>`;
      });
      html += "</tbody></table>";
      wrap.innerHTML = html;
    }

    function toggleDetail(id, row) {
      const el = document.getElementById(id);
      const open = el.classList.toggle("open");
      row.classList.toggle("open", open);
      row.querySelector(".chevron").innerHTML = open ? "&#9660;" : "&#9654;";
    }

    async function refresh() {
      try {
        const res = await fetch("/api/dashboard");
        const data = await res.json();
        renderCards(data);
        renderTable(data);
        const w = data.worker;
        let extra = "";
        if (w && w.last_scan_at) extra = " | worker scan: " + w.last_scan_at;
        document.getElementById("meta").textContent =
          "Updated " + (data.generated_at || "").replace("T", " ").slice(0, 19) + " UTC" +
          " | exec: " + (data.exec_mode || "?") + extra;
      } catch (e) {
        document.getElementById("meta").textContent = "Failed to load: " + e;
      }
    }

    refresh();
    setInterval(refresh, 30000);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    config: ArbConfig
    trade_limit: int = 50

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/dashboard"):
            self._send_html(DASHBOARD_HTML)
            return
        if path == "/api/dashboard":
            payload = build_dashboard_payload(self.config, trade_limit=self.trade_limit)
            self._send_json(payload)
            return
        if path == "/api/health":
            self._send_json({"ok": True})
            return
        self.send_error(404)


def run_dashboard(
    config: ArbConfig | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    trade_limit: int = 50,
) -> None:
    """Start blocking HTTP dashboard server."""
    cfg = config or ArbConfig.from_env()
    bind_host = host or os.environ.get("ARB_DASHBOARD_HOST", DEFAULT_HOST)
    bind_port = int(port or os.environ.get("ARB_DASHBOARD_PORT", str(DEFAULT_PORT)))

    handler = type(
        "BoundDashboardHandler",
        (DashboardHandler,),
        {"config": cfg, "trade_limit": trade_limit},
    )

    server = ThreadingHTTPServer((bind_host, bind_port), handler)
    print(f"Arb dashboard: http://{bind_host}:{bind_port}/")
    print(f"State DB: {cfg.state_db}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


def run_dashboard_background(
    config: ArbConfig | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
) -> threading.Thread:
    """Non-blocking dashboard (for tests)."""
    cfg = config or ArbConfig.from_env()
    bind_host = host or "127.0.0.1"
    bind_port = int(port or 0)

    handler = type("BgDashboardHandler", (DashboardHandler,), {"config": cfg, "trade_limit": 50})
    server = ThreadingHTTPServer((bind_host, bind_port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
