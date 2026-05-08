#!/usr/bin/env python3
"""
Prosperity Backtest Visualizer.

A local, zero-install web dashboard for inspecting IMC Prosperity backtest runs.
The server reads run folders that contain `metrics.json` and `submission.log`,
then serves an interactive Plotly dashboard in your browser.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections import defaultdict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


DEFAULT_PORT = 8766
TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    run_roots: tuple[Path, ...]
    open_browser: bool
    allow_delete: bool


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _path_label(path: Any) -> str:
    return str(path or "").replace("\\", "/")


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _dataset_label(metrics: dict[str, Any]) -> str:
    dataset_path = _path_label(metrics.get("dataset_path"))
    dataset_id = str(metrics.get("dataset_id") or "")
    day = metrics.get("day", "")
    day_label = f"day {day}" if day != "" else "day ?"

    if "/datasets/" in dataset_path:
        tail = dataset_path.split("/datasets/", 1)[1]
        parts = tail.split("/")
        if len(parts) >= 2 and parts[1].startswith("pct_"):
            return f"{parts[0]} / {parts[1]} / {day_label}"
        if parts and parts[0]:
            return f"{parts[0]} / {day_label}"

    if "/Data/" in dataset_path or "/data/" in dataset_path:
        return f"Data / {day_label}"

    return f"{dataset_id or 'unknown data'} / {day_label}"


def _run_base_and_day(run_dir: Path, metrics: dict[str, Any]) -> tuple[str, str]:
    full_name = run_dir.name
    parts = full_name.rsplit("-day", 1)
    if len(parts) > 1:
        return parts[0], parts[1]
    return full_name, str(metrics.get("day", "0"))


def _day_sort_key(day: dict[str, Any]) -> tuple[int, str]:
    value = str(day.get("day", ""))
    try:
        return int(value), value
    except ValueError:
        return 9999, value


def _unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    unique: list[Path] = []
    for raw in paths:
        path = raw.expanduser().resolve()
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return tuple(unique)


def default_run_roots(extra_roots: Iterable[Path] = ()) -> tuple[Path, ...]:
    candidates = [
        Path.cwd() / "runs",
        REPO_ROOT / "runs",
        REPO_ROOT / "prosperity_rust_backtester" / "runs",
        REPO_ROOT / "prosperity_rust_backtester-main" / "runs",
        TOOL_DIR / "runs",
        *extra_roots,
    ]
    return _unique_paths(candidates)


class RunStore:
    """Indexes backtest output directories and lazily loads day logs."""

    def __init__(self, run_roots: Iterable[Path]) -> None:
        self.run_roots = tuple(run_roots)
        self.runs_index: dict[str, dict[str, Any]] = {}
        self.runs_list: list[dict[str, Any]] = []
        self.day_dirs: set[str] = set()
        self._cache: dict[str, dict[str, Any]] = {}
        self.build_index()

    @property
    def day_count(self) -> int:
        return sum(len(run["days"]) for run in self.runs_list)

    def build_index(self) -> list[dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        day_dirs: set[str] = set()

        for runs_dir in self.run_roots:
            if not runs_dir.exists():
                continue
            for run_dir in sorted(runs_dir.iterdir(), key=lambda path: path.name):
                if not run_dir.is_dir():
                    continue

                metrics_file = run_dir / "metrics.json"
                if not metrics_file.exists():
                    continue

                metrics = _safe_read_json(metrics_file)
                if metrics is None:
                    continue

                base, day = _run_base_and_day(run_dir, metrics)
                resolved_run_dir = run_dir.resolve()
                day_dirs.add(str(resolved_run_dir))

                if base not in index:
                    index[base] = {
                        "id": base,
                        "days": [],
                        "total_pnl": 0.0,
                        "trader_path": metrics.get("trader_path", ""),
                        "generated_at": metrics.get("generated_at", ""),
                    }

                pnl = float(metrics.get("final_pnl_total") or 0.0)
                pnl_by_product = metrics.get("final_pnl_by_product") or {}
                if not isinstance(pnl_by_product, dict):
                    pnl_by_product = {}

                index[base]["days"].append(
                    {
                        "day": day,
                        "dir": str(resolved_run_dir),
                        "pnl": pnl,
                        "pnl_by_product": pnl_by_product,
                        "dataset": metrics.get("dataset_id", ""),
                        "dataset_path": metrics.get("dataset_path", ""),
                        "data_label": _dataset_label(metrics),
                        "trader_path": metrics.get("trader_path", ""),
                        "own_trade_count": metrics.get("own_trade_count"),
                        "tick_count": metrics.get("tick_count"),
                        "matching": metrics.get("matching", {}),
                    }
                )
                index[base]["total_pnl"] += pnl

        runs = sorted(index.values(), key=lambda run: run["total_pnl"], reverse=True)
        for run in runs:
            run["days"].sort(key=_day_sort_key)

        self.runs_index = index
        self.runs_list = runs
        self.day_dirs = day_dirs
        self._cache = {
            day_dir: payload
            for day_dir, payload in self._cache.items()
            if day_dir in self.day_dirs
        }
        return self.runs_list

    def clear_runs(self) -> dict[str, Any]:
        deleted = 0
        roots: list[str] = []

        for runs_dir in self.run_roots:
            if not runs_dir.exists():
                continue

            root = runs_dir.resolve()
            roots.append(str(root))
            for run_dir in runs_dir.iterdir():
                if not run_dir.is_dir():
                    continue
                if not (run_dir / "metrics.json").exists():
                    continue

                resolved_run_dir = run_dir.resolve()
                if resolved_run_dir.parent != root:
                    continue

                shutil.rmtree(resolved_run_dir)
                deleted += 1

        self._cache = {}
        self.build_index()
        return {"deleted": deleted, "roots": roots, "count": len(self.runs_list)}

    def load_day(self, dir_path: str) -> dict[str, Any] | None:
        try:
            resolved_dir = str(Path(dir_path).expanduser().resolve())
        except OSError:
            return None

        if resolved_dir not in self.day_dirs:
            return None

        if resolved_dir in self._cache:
            return self._cache[resolved_dir]

        log_file = Path(resolved_dir) / "submission.log"
        if not log_file.exists():
            return None

        raw = _safe_read_json(log_file)
        if raw is None:
            return None

        activities: dict[str, list[dict[str, Any]]] = defaultdict(list)
        activities_log = raw.get("activitiesLog") or ""
        reader = csv.DictReader(io.StringIO(str(activities_log)), delimiter=";")
        for row in reader:
            product = row.get("product") or "UNKNOWN"
            timestamp = _as_int(row.get("timestamp"))
            if timestamp is None:
                continue
            activities[product].append(
                {
                    "day": row.get("day"),
                    "ts": timestamp,
                    "mid": _as_float(row.get("mid_price")),
                    "pnl": _as_float(row.get("profit_and_loss")),
                    "b1": _as_float(row.get("bid_price_1")),
                    "a1": _as_float(row.get("ask_price_1")),
                    "b1v": _as_int(row.get("bid_volume_1")),
                    "a1v": _as_int(row.get("ask_volume_1")),
                    "b2": _as_float(row.get("bid_price_2")),
                    "a2": _as_float(row.get("ask_price_2")),
                    "b2v": _as_int(row.get("bid_volume_2")),
                    "a2v": _as_int(row.get("ask_volume_2")),
                    "b3": _as_float(row.get("bid_price_3")),
                    "a3": _as_float(row.get("ask_price_3")),
                    "b3v": _as_int(row.get("bid_volume_3")),
                    "a3v": _as_int(row.get("ask_volume_3")),
                }
            )

        trades: dict[str, list[dict[str, Any]]] = defaultdict(list)
        trade_history = raw.get("tradeHistory") or []
        if isinstance(trade_history, list):
            for trade in trade_history:
                if not isinstance(trade, dict):
                    continue
                product = trade.get("symbol") or trade.get("product") or "UNKNOWN"
                timestamp = _as_int(trade.get("timestamp"))
                if timestamp is None:
                    continue
                trades[product].append(
                    {
                        "ts": timestamp,
                        "price": _as_float(trade.get("price")),
                        "qty": _as_int(trade.get("quantity")),
                        "buyer": trade.get("buyer", ""),
                        "seller": trade.get("seller", ""),
                    }
                )

        result = {
            "_dir": resolved_dir,
            "products": sorted(activities.keys()),
            "activities": dict(activities),
            "trades": dict(trades),
        }
        self._cache[resolved_dir] = result
        return result

    def compare(self, run_ids: Iterable[str]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for run_id in run_ids:
            run = self.runs_index.get(run_id)
            if not run:
                continue

            pnl_by_product: dict[str, float] = {}
            for day in run["days"]:
                for product, value in (day.get("pnl_by_product") or {}).items():
                    try:
                        pnl_by_product[product] = pnl_by_product.get(product, 0.0) + float(value)
                    except (TypeError, ValueError):
                        continue

            output[run_id] = {
                "total_pnl": run["total_pnl"],
                "pnl_by_product": pnl_by_product,
            }
        return output


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prosperity Backtest Visualizer</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box}
:root{color-scheme:light dark}
body[data-theme="light"]{--bg:#eef2f6;--panel:#fbfcfe;--panel-2:#ffffff;--panel-3:#f5f7fa;--text:#111827;--muted:#667085;--faint:#98a2b3;--line:#d8dee8;--line-strong:#b7c1d0;--accent:#0f766e;--accent-2:#2563eb;--good:#12803c;--bad:#c43e37;--warn:#b76e00;--ink:#1f2937;--shadow:0 18px 45px rgba(17,24,39,.08);--plot:#ffffff;--plot-2:#f8fafc;--hover:#eef7f5}
body[data-theme="dark"]{--bg:#0b0f17;--panel:#111827;--panel-2:#151e2d;--panel-3:#0f1724;--text:#e7edf5;--muted:#94a3b8;--faint:#64748b;--line:#253248;--line-strong:#3a4a63;--accent:#2dd4bf;--accent-2:#60a5fa;--good:#4ade80;--bad:#fb7185;--warn:#fbbf24;--ink:#f8fafc;--shadow:0 22px 55px rgba(0,0,0,.32);--plot:#111827;--plot-2:#0f1724;--hover:#172536}
body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);height:100vh;overflow:hidden}
button,input,select{font:inherit}
button{border:0;background:transparent;color:inherit}
.icon-btn svg,.toggle-btn svg{width:16px;height:16px;stroke-width:1.9}
#app{height:100vh;display:grid;grid-template-columns:var(--sidebar-w,380px) 8px minmax(0,1fr);min-width:0}
#sidebar{min-width:0;background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column;overflow:hidden}
#sidebar-resizer{background:var(--bg);border-left:1px solid var(--line);border-right:1px solid var(--line);cursor:col-resize;position:relative}
#sidebar-resizer::after{content:"";position:absolute;top:50%;left:50%;width:2px;height:42px;border-radius:999px;background:var(--line-strong);transform:translate(-50%,-50%)}
#sidebar-resizer:hover::after,#sidebar-resizer.resizing::after{background:var(--accent-2)}
#sidebar-header{height:64px;padding:12px 14px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:12px}
#brand{min-width:0}
#sidebar-title{font-size:15px;font-weight:780;color:var(--ink);letter-spacing:.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#sidebar-subtitle{font-size:11px;color:var(--muted);margin-top:2px}
#run-count{font-weight:700;color:var(--accent);margin-left:4px}
#sidebar-actions,.toolbar-actions,#compare-actions{display:flex;gap:6px;align-items:center}
.toolbar-divider{width:1px;height:26px;background:var(--line);margin:0 2px}
.icon-btn,.toggle-btn{width:34px;height:34px;display:inline-grid;place-items:center;border:1px solid var(--line);border-radius:8px;background:var(--panel-2);color:var(--muted);cursor:pointer;transition:background .16s,border-color .16s,color .16s,transform .16s}
.icon-btn:hover,.toggle-btn:hover{border-color:var(--accent-2);color:var(--accent-2);transform:translateY(-1px)}
.icon-btn.danger:hover{border-color:var(--bad);color:var(--bad)}
.toggle-btn[aria-pressed="true"]{background:color-mix(in srgb,var(--accent-2) 15%,var(--panel-2));border-color:var(--accent-2);color:var(--accent-2)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
#search-row{padding:10px 12px;border-bottom:1px solid var(--line);background:var(--panel-3);display:flex;gap:8px;align-items:center}
#search{min-width:0;flex:1;height:36px;padding:0 11px;background:var(--panel-2);border:1px solid var(--line);border-radius:8px;color:var(--text);font-size:13px;outline:none}
#search:focus{border-color:var(--accent-2);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent-2) 16%,transparent)}
#run-list{flex:1;overflow-y:auto;overflow-x:hidden}
.run-item{padding:12px 12px;border-bottom:1px solid var(--line);display:grid;grid-template-columns:34px minmax(0,1fr) minmax(108px,max-content);gap:6px 10px;align-items:start;cursor:pointer}
.run-item:hover,.run-item.active{background:var(--hover)}
.run-item.compare-selected{box-shadow:inset 3px 0 0 var(--accent-2);background:color-mix(in srgb,var(--accent-2) 9%,var(--panel))}
.run-pnl{grid-column:3;font-size:17px;font-weight:800;line-height:1.1;text-align:right;justify-self:end;max-width:150px;overflow:hidden;text-overflow:clip;white-space:nowrap;font-variant-numeric:tabular-nums}
.run-pnl.pos,.pos{color:var(--good)}.run-pnl.neg,.neg{color:var(--bad)}
.run-id{font-size:12px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;grid-column:1 / span 3}
.run-meta{font-size:12px;color:var(--faint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;grid-column:1 / span 3}
.compare-toggle{grid-row:1;grid-column:1;width:32px;height:32px}
#compare-bar{min-height:52px;padding:9px 12px;background:var(--panel-3);border-top:1px solid var(--line);display:flex;align-items:center;gap:8px;color:var(--muted)}
#cmp-label{flex:1;min-width:0;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#main{min-width:0;display:flex;flex-direction:column;overflow:hidden}
#toolbar{min-height:64px;padding:10px 14px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.field{height:38px;display:flex;align-items:center;gap:8px;background:var(--panel-2);border:1px solid var(--line);border-radius:8px;padding:0 8px;min-width:0}
.field-label{font-size:11px;font-weight:760;color:var(--faint);text-transform:uppercase}
#toolbar select{min-width:120px;max-width:min(420px,42vw);background:var(--panel-2);border:0;color:var(--text);font-size:13px;outline:none;color-scheme:dark;padding:0 2px}
body[data-theme="light"] #toolbar select{color-scheme:light}
#toolbar option{background:var(--panel-2);color:var(--text)}
#toolbar-pnl{margin-left:auto;font-weight:780;font-size:13px;min-width:220px;text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#charts{flex:1;min-height:0;overflow:auto;padding:14px;display:grid;grid-template-columns:minmax(0,1fr);gap:12px}
.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.summary-card,.chart-wrap,.table-wrap{background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:var(--shadow)}
.summary-card{padding:11px 12px;min-width:0}
.summary-label{font-size:10px;color:var(--faint);font-weight:800;text-transform:uppercase;letter-spacing:.06em}
.summary-value{font-size:18px;font-weight:820;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.summary-sub{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.chart-wrap{padding:8px;min-width:0}
.chart-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px;padding:0 2px}
.chart-title{font-size:12px;color:var(--ink);font-weight:800;text-transform:uppercase;letter-spacing:.04em}
.chart-note{font-size:11px;color:var(--faint);white-space:nowrap}
.chart-div{width:100%;min-width:0}
.table-wrap{padding:10px;min-width:0}
.table-title{font-size:12px;color:var(--ink);font-weight:800;text-transform:uppercase;letter-spacing:.04em;margin:0 0 8px}
.table-scroller{overflow-x:auto}
.data-table{width:100%;border-collapse:collapse;font-size:12px;min-width:760px}
.data-table th{color:var(--faint);font-size:10px;text-transform:uppercase;letter-spacing:.05em;text-align:right;padding:7px 8px;border-bottom:1px solid var(--line)}
.data-table th:first-child,.data-table td:first-child{text-align:left}
.data-table td{padding:7px 8px;border-bottom:1px solid color-mix(in srgb,var(--line) 75%,transparent);text-align:right;white-space:nowrap}
.data-table tr:last-child td{border-bottom:0}
.data-table tbody tr:hover{background:var(--panel-3)}
.pill{display:inline-flex;align-items:center;gap:5px;height:22px;padding:0 8px;border-radius:999px;background:var(--panel-3);border:1px solid var(--line);color:var(--muted);font-size:11px}
#placeholder{display:flex;align-items:center;justify-content:center;min-height:360px;color:var(--muted);font-size:16px;text-align:center;padding:24px;background:var(--panel);border:1px solid var(--line);border-radius:8px}
.chart-error{height:100%;min-height:220px;display:flex;align-items:center;justify-content:center;color:var(--bad);font-size:13px;text-align:center;padding:20px}
@media (max-width: 920px){body{height:auto;overflow:auto}#app{height:auto;display:block}#sidebar{height:45vh;border-right:0;border-bottom:1px solid var(--line)}#sidebar-resizer{display:none}#main{height:auto;min-height:55vh}#toolbar-pnl{margin-left:0;text-align:left;width:100%}#charts{overflow:visible}#toolbar select{max-width:58vw}}
</style>
</head>
<body data-theme="dark">
<div id="app">
  <aside id="sidebar">
    <div id="sidebar-header">
      <div id="brand">
        <div id="sidebar-title">Prosperity Visualizer <span id="run-count"></span></div>
        <div id="sidebar-subtitle">Run lab for backtests, PnL, fills, and edge attribution</div>
      </div>
      <div id="sidebar-actions">
        <button id="theme-btn" class="icon-btn" type="button" title="Toggle theme" aria-label="Toggle theme"><i data-lucide="moon"></i></button>
        <button id="refresh-btn" class="icon-btn" type="button" title="Refresh runs" aria-label="Refresh runs"><i data-lucide="refresh-cw"></i></button>
        <button id="clear-runs-btn" class="icon-btn danger" type="button" title="Delete indexed runs" aria-label="Delete indexed runs"><i data-lucide="trash-2"></i></button>
      </div>
    </div>
    <div id="search-row">
      <input id="search" placeholder="Filter runs, traders, datasets, products">
      <button id="clear-search-btn" class="icon-btn" type="button" title="Clear search" aria-label="Clear search"><i data-lucide="x"></i></button>
    </div>
    <div id="run-list"></div>
    <div id="compare-bar">
      <span id="cmp-label">Select runs for overlay comparison</span>
      <div id="compare-actions">
        <button id="cmp-btn" class="icon-btn" type="button" style="display:none" title="Open comparison" aria-label="Open comparison"><i data-lucide="chart-line"></i></button>
        <button id="cmp-clr" class="icon-btn" type="button" style="display:none" title="Clear comparison" aria-label="Clear comparison"><i data-lucide="rotate-ccw"></i></button>
      </div>
    </div>
  </aside>
  <div id="sidebar-resizer" title="Resize sidebar" aria-hidden="true"></div>
  <main id="main">
    <div id="toolbar">
      <div class="field"><span class="field-label">Day</span><select id="day-sel"></select></div>
      <div class="field"><span class="field-label">View</span><select id="prod-sel"></select></div>
      <div class="toolbar-actions">
        <button id="show-bot-btn" class="toggle-btn" type="button" title="Show bot trades and bot data" aria-label="Show bot trades and bot data" aria-pressed="true"><i data-lucide="bot"></i></button>
        <button id="show-market-btn" class="toggle-btn" type="button" title="Show non-submission market trades" aria-label="Show non-submission market trades" aria-pressed="false"><i data-lucide="activity"></i></button>
        <button id="show-spread-btn" class="toggle-btn" type="button" title="Show best bid and ask" aria-label="Show best bid and ask" aria-pressed="false"><i data-lucide="scan-line"></i></button>
        <span class="toolbar-divider" aria-hidden="true"></span>
        <button id="show-risk-btn" class="toggle-btn" type="button" title="Show risk analytics" aria-label="Show risk analytics" aria-pressed="true"><i data-lucide="shield-alert"></i></button>
        <button id="show-execution-btn" class="toggle-btn" type="button" title="Show execution analytics" aria-label="Show execution analytics" aria-pressed="true"><i data-lucide="crosshair"></i></button>
        <button id="show-book-btn" class="toggle-btn" type="button" title="Show order book analytics" aria-label="Show order book analytics" aria-pressed="true"><i data-lucide="layers"></i></button>
        <button id="show-relation-btn" class="toggle-btn" type="button" title="Show cross-product analytics" aria-label="Show cross-product analytics" aria-pressed="true"><i data-lucide="network"></i></button>
        <button id="autoscale-btn" class="icon-btn" type="button" title="Autoscale charts" aria-label="Autoscale charts"><i data-lucide="maximize-2"></i></button>
      </div>
      <span id="toolbar-pnl"></span>
    </div>
    <div id="charts"><div id="placeholder">Loading runs...</div></div>
  </main>
</div>
<script>
let allRuns=[];
let config={allow_delete:false,run_roots:[]};
let currentRun=null;
let currentData=null;
let preferredDayKey=null;
let compareSet=new Set();
let activeMode="run";
let showBotData=true;
let showMarketTrades=false;
let showSpread=false;
let showRiskAnalytics=true;
let showExecutionAnalytics=true;
let showBookAnalytics=true;
let showRelationAnalytics=true;

const COLORS=["#60a5fa","#2dd4bf","#f59e0b","#f472b6","#a78bfa","#fb7185","#22d3ee","#84cc16","#f97316","#c084fc","#14b8a6","#eab308"];
const PLOT_CONFIG={responsive:true,displaylogo:false,modeBarButtonsToRemove:["lasso2d","select2d"]};

function cssVar(name){return getComputedStyle(document.body).getPropertyValue(name).trim()}
function mergeAxis(base,extra){return Object.assign({},base||{},extra||{})}
function layout(extra={}){
  const base={
    paper_bgcolor:cssVar("--plot"),
    plot_bgcolor:cssVar("--plot-2"),
    font:{color:cssVar("--muted"),size:11},
    margin:{t:20,b:42,l:62,r:22},
    xaxis:{gridcolor:cssVar("--line"),zerolinecolor:cssVar("--line-strong")},
    yaxis:{gridcolor:cssVar("--line"),zerolinecolor:cssVar("--line-strong")},
    showlegend:true,
    legend:{bgcolor:"rgba(0,0,0,0)",font:{size:10}},
  };
  const out=Object.assign({},base,extra);
  out.xaxis=mergeAxis(base.xaxis,extra.xaxis);
  out.yaxis=mergeAxis(base.yaxis,extra.yaxis);
  out.xaxis.linecolor=cssVar("--line-strong");
  out.yaxis.linecolor=cssVar("--line-strong");
  out.xaxis.tickfont={color:cssVar("--muted")};
  out.yaxis.tickfont={color:cssVar("--muted")};
  out.autosize=true;
  return out;
}
function pnlClass(pnl){return Number(pnl)>=0?"pos":"neg"}
function fmt(n,digits=0){return Number(n||0).toLocaleString(undefined,{maximumFractionDigits:digits})}
function signed(n,digits=0){const value=Number(n||0);return `${value>=0?"+":""}${fmt(value,digits)}`}
function pct(n,digits=1){return Number.isFinite(Number(n))?`${fmt(Number(n)*100,digits)}%`:"-"}
function runLabel(id){return String(id||"").replace("backtest-","").slice(0,30)}
function esc(value){return String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]))}
function basename(path){const parts=String(path||"").replace(/\\/g,"/").split("/");return parts[parts.length-1]||String(path||"")}
function dataLabel(day){return day?.data_label||day?.dataset_path||day?.dataset||`day ${day?.day??"?"}`}
function traderLabel(run,day){return basename(day?.trader_path||run?.trader_path)||"unknown trader"}
function currentDayMeta(){return currentRun?.days.find(d=>d.dir===currentData?._dir)}
function icon(name){return `<i data-lucide="${name}"></i>`}
function syncIcons(){if(window.lucide)window.lucide.createIcons()}
function setPressed(id,value){document.getElementById(id).setAttribute("aria-pressed",value?"true":"false")}
function toggleTheme(){
  const next=document.body.dataset.theme==="dark"?"light":"dark";
  applyTheme(next);
  redrawActiveView();
}
function applyTheme(theme){
  document.body.dataset.theme=theme;
  localStorage.setItem("prosperity-theme",theme);
  const themeBtn=document.getElementById("theme-btn");
  themeBtn.innerHTML=icon(theme==="dark"?"sun":"moon");
  themeBtn.title=theme==="dark"?"Switch to light mode":"Switch to dark mode";
  syncIcons();
}
function initTheme(){
  const saved=localStorage.getItem("prosperity-theme");
  const prefersLight=window.matchMedia&&window.matchMedia("(prefers-color-scheme: light)").matches;
  applyTheme(saved||(prefersLight?"light":"dark"));
}
function initSidebarResize(){
  const app=document.getElementById("app");
  const handle=document.getElementById("sidebar-resizer");
  const saved=Number(localStorage.getItem("prosperity-sidebar-width"));
  if(Number.isFinite(saved))app.style.setProperty("--sidebar-w",`${Math.min(620,Math.max(300,saved))}px`);
  let dragging=false;
  function setWidth(clientX){
    const width=Math.min(620,Math.max(300,clientX));
    app.style.setProperty("--sidebar-w",`${width}px`);
    localStorage.setItem("prosperity-sidebar-width",String(Math.round(width)));
    resizePlots();
  }
  handle.addEventListener("pointerdown",event=>{
    dragging=true;
    handle.classList.add("resizing");
    handle.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  handle.addEventListener("pointermove",event=>{
    if(dragging)setWidth(event.clientX);
  });
  handle.addEventListener("pointerup",event=>{
    dragging=false;
    handle.classList.remove("resizing");
    handle.releasePointerCapture(event.pointerId);
  });
  handle.addEventListener("pointercancel",()=>{
    dragging=false;
    handle.classList.remove("resizing");
  });
}
function dayKey(day){
  const label=String(dataLabel(day)||"").toLowerCase();
  let match=label.match(/day\s*\+?(-?\d+)/);
  if(match)return `day_${match[1]}`;
  const raw=String(day?.day??"").toLowerCase();
  match=raw.match(/\+?(-?\d+)/);
  if(match)return `day_${match[1]}`;
  const path=String(day?.dataset_path||day?.dataset||"").toLowerCase();
  match=path.match(/day_(-?\d+)/);
  return match?`day_${match[1]}`:"";
}
function finite(values){return values.filter(v=>Number.isFinite(v))}
function minMax(values){
  let lo=Infinity,hi=-Infinity;
  values.forEach(value=>{
    const n=Number(value);
    if(!Number.isFinite(n))return;
    lo=Math.min(lo,n);
    hi=Math.max(hi,n);
  });
  return lo===Infinity?null:{lo,hi};
}
function rangeFor(values,{includeZero=false,pad=0.06}={}){
  const nums=finite(values);
  if(includeZero)nums.push(0);
  if(!nums.length)return undefined;
  let {lo,hi}=minMax(nums);
  if(lo===hi){
    const bump=Math.max(1,Math.abs(lo)*0.02);
    lo-=bump;hi+=bump;
  }else{
    const bump=(hi-lo)*pad;
    lo-=bump;hi+=bump;
  }
  return [lo,hi];
}
function axisFrom(values,opts={}){
  const range=rangeFor(values,opts);
  return range?{range,autorange:false}:{autorange:true};
}
function traceValues(traces,key){
  return traces.flatMap(t=>(t[key]||[]).map(Number)).filter(Number.isFinite);
}
function plot(el,traces,extra={},opts={}){
  if(!window.Plotly){
    el.innerHTML='<div class="chart-error">Plotly could not load. Check your internet connection or vendor Plotly locally.</div>';
    return;
  }
  try{
    const xaxis=Object.assign(axisFrom(traceValues(traces,"x")),extra.xaxis||{});
    const yaxis=opts.autoY
      ? Object.assign({autorange:true},extra.yaxis||{})
      : Object.assign(axisFrom(traceValues(traces,"y"),{includeZero:!!opts.includeZeroY}),extra.yaxis||{});
    return Plotly.newPlot(el,traces,layout(Object.assign({},extra,{xaxis,yaxis})),PLOT_CONFIG).then(()=>{
      requestAnimationFrame(()=>Plotly.Plots.resize(el));
    }).catch(error=>{
      console.error("Plotly render failed",error);
      el.innerHTML=`<div class="chart-error">Chart render failed: ${esc(error.message||error)}</div>`;
    });
  }catch(error){
    console.error("Chart setup failed",error);
    el.innerHTML=`<div class="chart-error">Chart setup failed: ${esc(error.message||error)}</div>`;
  }
}
function resizePlots(){
  if(!window.Plotly)return;
  document.querySelectorAll(".js-plotly-plot").forEach(el=>Plotly.Plots.resize(el));
}
async function fetchJson(url,options={}){
  const res=await fetch(url,options);
  if(!res.ok)throw new Error(`${res.status} ${res.statusText}`);
  return await res.json();
}
function selectedProduct(){
  return document.getElementById("prod-sel").value||"[TOTAL]";
}
function selectedCompareDayKey(){
  return preferredDayKey||dayKey(currentDayMeta())||"";
}
function chooseDayForRun(run){
  const wanted=selectedCompareDayKey();
  return run.days.find(day=>dayKey(day)===wanted)||run.days[0];
}
function redrawActiveView(){
  if(activeMode==="compare"&&compareSet.size>1)openCompare();
  else drawCharts();
}

async function init(){
  try{
    initTheme();
    initSidebarResize();
    syncIcons();
    config=await fetchJson("/api/config");
    document.getElementById("clear-runs-btn").style.display=config.allow_delete?"":"none";
    await refreshRuns({preserveSelection:false});
  }catch(error){
    resetSelection(`Visualizer failed to initialize: ${error.message}`);
  }
}
async function refreshRuns({preserveSelection=true}={}){
  const btn=document.getElementById("refresh-btn");
  const selectedId=preserveSelection&&currentRun?currentRun.id:null;
  btn.classList.add("working");
  btn.disabled=true;
  try{
    await fetchJson("/api/refresh");
    allRuns=await fetchJson("/api/runs");
    document.getElementById("run-count").textContent=`(${allRuns.length})`;
    const visible=filterRuns();
    if(selectedId&&allRuns.some(run=>run.id===selectedId))await selectRun(selectedId);
    else if(visible.length)await selectRun(visible[0].id);
    else resetSelection("No runs found. Run a backtest, then press Refresh.");
  }finally{
    btn.disabled=false;
    btn.classList.remove("working");
    syncIcons();
  }
}
async function clearRuns(){
  if(!config.allow_delete)return;
  if(!confirm("Delete all indexed run folders? This only removes directories containing metrics.json inside configured run roots."))return;
  const btn=document.getElementById("clear-runs-btn");
  btn.disabled=true;
  try{
    const data=await fetchJson("/api/clear-runs",{method:"POST"});
    allRuns=[];
    compareSet.clear();
    document.getElementById("run-count").textContent="(0)";
    resetSelection(`Deleted ${data.deleted} run folders.`);
    filterRuns();
  }catch(error){
    resetSelection(`Delete failed: ${error.message}`);
  }finally{
    btn.disabled=false;
    syncIcons();
  }
}
function resetSelection(message){
  currentRun=null;
  currentData=null;
  document.getElementById("day-sel").innerHTML="";
  document.getElementById("prod-sel").innerHTML="";
  document.getElementById("toolbar-pnl").textContent="";
  document.getElementById("charts").innerHTML=`<div id="placeholder">${esc(message)}</div>`;
}
function clearSearch(){
  document.getElementById("search").value="";
  const visible=filterRuns();
  if(visible.length&&!currentRun)selectRun(visible[0].id);
}
function renderList(runs){
  const el=document.getElementById("run-list");
  if(!runs.length){
    el.innerHTML='<div style="padding:16px;color:var(--muted)">No matching runs.</div>';
    return;
  }
  el.innerHTML=runs.map(run=>{
    const pnl=Number(run.total_pnl||0);
    const datasets=[...new Set(run.days.map(dataLabel))].slice(0,2).join(" | ");
    const extra=run.days.length>2?" ...":"";
    const active=currentRun&&currentRun.id===run.id?" active":"";
    const selected=compareSet.has(run.id)?" compare-selected":"";
    const compareIcon=compareSet.has(run.id)?"check":"plus";
    return `<div class="run-item${active}${selected}" data-run-id="${esc(run.id)}">
      <button class="icon-btn compare-toggle" type="button" data-compare-id="${esc(run.id)}" title="Toggle comparison" aria-label="Toggle comparison">${icon(compareIcon)}</button>
      <div class="run-pnl ${pnlClass(pnl)}" title="${signed(pnl)}">${signed(pnl)}</div>
      <div class="run-id">${esc(runLabel(run.id))}</div>
      <div class="run-meta">Trader: ${esc(traderLabel(run,run.days[0]))}</div>
      <div class="run-meta">Data: ${esc(datasets+extra)}</div>
    </div>`;
  }).join("");
  syncIcons();
}
function filterRuns(){
  const q=document.getElementById("search").value.toLowerCase();
  const filtered=allRuns.filter(run=>
    run.id.toLowerCase().includes(q)||
    String(run.trader_path).toLowerCase().includes(q)||
    run.days.some(day=>
      String(dataLabel(day)).toLowerCase().includes(q)||
      String(day.dataset_path||"").toLowerCase().includes(q)||
      Object.keys(day.pnl_by_product||{}).some(product=>product.toLowerCase().includes(q))
    )
  );
  renderList(filtered);
  return filtered;
}
async function selectRun(id){
  activeMode="run";
  currentRun=allRuns.find(run=>run.id===id);
  if(!currentRun)return;
  currentData=null;
  filterRuns();
  const daySel=document.getElementById("day-sel");
  daySel.innerHTML=currentRun.days.map(day=>`<option value="${encodeURIComponent(day.dir)}">${esc(dataLabel(day))} | ${esc(traderLabel(currentRun,day))}</option>`).join("");
  const preferred=currentRun.days.find(day=>dayKey(day)===preferredDayKey);
  if(preferred)daySel.value=encodeURIComponent(preferred.dir);
  await loadDay(false);
}
async function loadDay(userSelected=false){
  activeMode="run";
  if(!currentRun)return;
  const encoded=document.getElementById("day-sel").value;
  if(!encoded)return;
  const dir=decodeURIComponent(encoded);
  const dayMeta=currentRun.days.find(day=>day.dir===dir);
  if(userSelected&&dayMeta)preferredDayKey=dayKey(dayMeta)||preferredDayKey;
  try{
    currentData=await fetchJson("/api/day?dir="+encodeURIComponent(dir));
  }catch(error){
    document.getElementById("charts").innerHTML='<div id="placeholder">No readable submission.log found for this run.</div>';
    return;
  }
  currentData._dir=dir;
  const prodSel=document.getElementById("prod-sel");
  const previous=prodSel.value;
  const products=["[TOTAL]","[ALL PRODUCTS]",...currentData.products];
  prodSel.innerHTML=products.map(product=>`<option value="${esc(product)}" ${product===previous?"selected":""}>${esc(product)}</option>`).join("");
  if(!products.includes(previous))prodSel.value="[TOTAL]";
  drawCharts();
}
function makeChartWrap(parent,title,height=300){
  const wrap=document.createElement("div");
  wrap.className="chart-wrap";
  const head=document.createElement("div");
  head.className="chart-head";
  const titleEl=document.createElement("div");
  titleEl.className="chart-title";
  titleEl.textContent=title;
  const note=document.createElement("div");
  note.className="chart-note";
  note.textContent="autoscaled";
  const div=document.createElement("div");
  div.className="chart-div";
  div.style.height=height+"px";
  head.appendChild(titleEl);
  head.appendChild(note);
  wrap.appendChild(head);
  wrap.appendChild(div);
  parent.appendChild(wrap);
  return div;
}
function makeTableWrap(parent,title){
  const wrap=document.createElement("div");
  wrap.className="table-wrap";
  const heading=document.createElement("div");
  heading.className="table-title";
  heading.textContent=title;
  const scroller=document.createElement("div");
  scroller.className="table-scroller";
  wrap.appendChild(heading);
  wrap.appendChild(scroller);
  parent.appendChild(wrap);
  return scroller;
}
function drawCharts(){
  if(!currentData)return;
  activeMode="run";
  const prodSel=selectedProduct();
  const isAggregate=prodSel==="[TOTAL]"||prodSel==="[ALL PRODUCTS]";
  const prods=isAggregate?currentData.products:[prodSel];
  const charts=document.getElementById("charts");
  charts.innerHTML="";
  makeSummary(charts,prods,prodSel);
  if(prodSel==="[TOTAL]"){
    makePnlChart(charts,prods,{totalOnly:true});
    makeProductPnlBar(charts,prods);
    if(showRiskAnalytics)makeRiskAnalytics(charts,prods,prodSel);
    if(showExecutionAnalytics)makeExecutionAnalytics(charts,prods,prodSel);
    if(showBookAnalytics)makeBookAnalytics(charts,prods,prodSel);
    if(showRelationAnalytics)makeRelationAnalytics(charts,prods,prodSel);
    makeProductTable(charts,prods);
  }else if(prodSel==="[ALL PRODUCTS]"){
    makePnlChart(charts,prods,{topProducts:8});
    makeProductPnlBar(charts,prods);
    if(showRiskAnalytics)makeRiskAnalytics(charts,prods,prodSel);
    if(showExecutionAnalytics)makeExecutionAnalytics(charts,prods,prodSel);
    if(showBookAnalytics)makeBookAnalytics(charts,prods,prodSel);
    if(showRelationAnalytics)makeRelationAnalytics(charts,prods,prodSel);
    makeProductTable(charts,prods);
  }else{
    makePnlChart(charts,prods);
    makeSingleProductCharts(charts,prodSel);
    if(showRiskAnalytics)makeRiskAnalytics(charts,prods,prodSel);
    if(showExecutionAnalytics)makeExecutionAnalytics(charts,prods,prodSel);
    if(showBookAnalytics)makeBookAnalytics(charts,prods,prodSel);
    makeProductTable(charts,prods);
    if(showBotData)makeTradeTape(charts,prodSel);
  }
  const dayMeta=currentDayMeta();
  if(dayMeta){
    const pnl=Number(dayMeta.pnl||0);
    const el=document.getElementById("toolbar-pnl");
    el.textContent=`${dataLabel(dayMeta)} | ${traderLabel(currentRun,dayMeta)} | PnL ${signed(pnl)}`;
    el.style.color=pnl>=0?cssVar("--good"):cssVar("--bad");
  }
  syncIcons();
  setTimeout(resizePlots,0);
  setTimeout(resizePlots,250);
}
function makeSummary(parent,prods,prodSel){
  const dayMeta=currentDayMeta();
  const grid=document.createElement("div");
  grid.className="summary-grid";
  const stats=prods.map(product=>productStats(currentData,product,dayMeta));
  const activities=stats.reduce((sum,row)=>sum+row.points,0);
  const botTrades=stats.reduce((sum,row)=>sum+row.botTrades,0);
  const marketTrades=stats.reduce((sum,row)=>sum+row.marketTrades,0);
  const netQty=stats.reduce((sum,row)=>sum+row.netQty,0);
  const grossNotional=stats.reduce((sum,row)=>sum+row.grossNotional,0);
  const maxDrawdown=Math.max(0,...stats.map(row=>row.drawdown));
  const avgSpread=avg(stats.map(row=>row.avgSpread).filter(Number.isFinite));
  const pnlByProduct=dayMeta?.pnl_by_product||{};
  const selectedPnls=prods.map(product=>[product,Number(pnlByProduct[product]??0)]).sort((a,b)=>b[1]-a[1]);
  const best=selectedPnls[0]||["-",0];
  const worst=selectedPnls[selectedPnls.length-1]||["-",0];
  const selection=prodSel==="[TOTAL]"?"Total PnL":prodSel==="[ALL PRODUCTS]"?`${prods.length} products`:prodSel;
  const cards=[
    ["Selection",selection,`${fmt(activities)} activity points`],
    ["Final PnL",signed(dayMeta?.pnl||selectedPnls.reduce((sum,row)=>sum+row[1],0)),dataLabel(dayMeta)],
    ["Max Drawdown",fmt(maxDrawdown),`Across ${prods.length} product${prods.length===1?"":"s"}`],
    ["Bot Trades",showBotData?fmt(botTrades):"Hidden",`Net qty ${fmt(netQty)} | gross ${fmt(grossNotional)}`],
    ["Market Trades",showMarketTrades?fmt(marketTrades):"Hidden","Non-submission prints"],
    ["Avg Spread",Number.isFinite(avgSpread)?fmt(avgSpread,2):"-","Best ask minus best bid"],
    ["Best Product",`${best[0]} ${signed(best[1])}`,"Highest final product PnL"],
    ["Worst Product",`${worst[0]} ${signed(worst[1])}`,"Lowest final product PnL"],
  ];
  grid.innerHTML=cards.map(([label,value,sub])=>`<div class="summary-card"><div class="summary-label">${esc(label)}</div><div class="summary-value" title="${esc(value)}">${esc(value)}</div><div class="summary-sub" title="${esc(sub)}">${esc(sub)}</div></div>`).join("");
  parent.appendChild(grid);
}
function avg(values){
  const nums=values.filter(value=>Number.isFinite(Number(value))).map(Number);
  return nums.length?nums.reduce((sum,value)=>sum+value,0)/nums.length:NaN;
}
function lastFinite(values){
  for(let i=values.length-1;i>=0;i--){
    const value=Number(values[i]);
    if(Number.isFinite(value))return value;
  }
  return 0;
}
function maxDrawdown(values){
  const nums=values.map(Number).filter(Number.isFinite);
  if(!nums.length)return 0;
  let peak=nums[0],dd=0;
  nums.forEach(value=>{
    peak=Math.max(peak,value);
    dd=Math.max(dd,peak-value);
  });
  return dd;
}
function stdev(values){
  const nums=values.map(Number).filter(Number.isFinite);
  if(nums.length<2)return NaN;
  const mean=avg(nums);
  const variance=nums.reduce((sum,value)=>sum+(value-mean)**2,0)/(nums.length-1);
  return Math.sqrt(variance);
}
function sortedActivities(data,product){
  return [...(data.activities[product]||[])].sort((a,b)=>Number(a.ts)-Number(b.ts));
}
function sortedTrades(data,product){
  return [...(data.trades[product]||[])].sort((a,b)=>Number(a.ts)-Number(b.ts));
}
function botTradesFor(data,product){
  return sortedTrades(data,product).filter(trade=>trade.buyer==="SUBMISSION"||trade.seller==="SUBMISSION");
}
function signedTradeQty(trade){
  const qty=Number(trade.qty||0);
  if(trade.buyer==="SUBMISSION")return qty;
  if(trade.seller==="SUBMISSION")return -qty;
  return 0;
}
function nearestActivityIndex(activities,ts){
  if(!activities.length)return -1;
  const target=Number(ts);
  if(!Number.isFinite(target))return -1;
  let lo=0,hi=activities.length-1;
  if(target<=Number(activities[lo].ts))return lo;
  if(target>=Number(activities[hi].ts))return hi;
  while(lo<=hi){
    const mid=Math.floor((lo+hi)/2);
    const value=Number(activities[mid].ts);
    if(value===target)return mid;
    if(value<target)lo=mid+1;
    else hi=mid-1;
  }
  const before=Math.max(0,lo-1);
  const after=Math.min(activities.length-1,lo);
  return Math.abs(Number(activities[before].ts)-target)<=Math.abs(Number(activities[after].ts)-target)?before:after;
}
function getTradeAnalytics(data,product){
  data._tradeAnalytics=data._tradeAnalytics||{};
  if(data._tradeAnalytics[product])return data._tradeAnalytics[product];
  const activities=sortedActivities(data,product);
  const horizons=[1,5,10,50];
  const rows=botTradesFor(data,product).map(trade=>{
    const index=nearestActivityIndex(activities,trade.ts);
    const point=activities[index]||{};
    const side=botSide(trade);
    const price=Number(trade.price);
    const qty=Number(trade.qty||0);
    const mid=Number(point.mid);
    const b1=Number(point.b1);
    const a1=Number(point.a1);
    const spread=Number.isFinite(a1)&&Number.isFinite(b1)?a1-b1:NaN;
    const edge=side==="BUY"?mid-price:price-mid;
    const spreadCapture=Number.isFinite(spread)&&spread!==0?edge/spread:NaN;
    const crossed=side==="BUY"
      ? Number.isFinite(a1)&&price>=a1
      : Number.isFinite(b1)&&price<=b1;
    const markouts={};
    horizons.forEach(horizon=>{
      const future=activities[Math.min(activities.length-1,index+horizon)]||{};
      const futureMid=Number(future.mid);
      markouts[horizon]=side==="BUY"?futureMid-price:price-futureMid;
    });
    return {
      product,
      ts:Number(trade.ts),
      side,
      price,
      qty,
      buyer:trade.buyer||"",
      seller:trade.seller||"",
      mid,
      b1,
      a1,
      spread,
      edge,
      spreadCapture,
      crossed,
      markouts,
      notional:Math.abs(price*qty),
    };
  }).filter(row=>row.side!=="MKT");
  data._tradeAnalytics[product]=rows;
  return rows;
}
function inventorySeries(data,product){
  data._inventorySeries=data._inventorySeries||{};
  if(data._inventorySeries[product])return data._inventorySeries[product];
  const activities=sortedActivities(data,product);
  const trades=botTradesFor(data,product);
  let tradeIndex=0;
  let position=0;
  let cash=0;
  const x=[],pos=[],exposure=[],mtm=[];
  activities.forEach(point=>{
    const ts=Number(point.ts);
    while(tradeIndex<trades.length&&Number(trades[tradeIndex].ts)<=ts){
      const trade=trades[tradeIndex];
      const qty=signedTradeQty(trade);
      const price=Number(trade.price||0);
      position+=qty;
      cash-=qty*price;
      tradeIndex+=1;
    }
    const mid=Number(point.mid);
    x.push(ts);
    pos.push(position);
    exposure.push(Number.isFinite(mid)?Math.abs(position*mid):NaN);
    mtm.push(Number.isFinite(mid)?cash+position*mid:NaN);
  });
  const result={x,pos,exposure,mtm};
  data._inventorySeries[product]=result;
  return result;
}
function depthValue(point,prefix,levels){
  let total=0;
  for(let level=1;level<=levels;level++){
    const price=Number(point[`${prefix}${level}`]);
    const volume=Number(point[`${prefix}${level}v`]);
    if(Number.isFinite(price)&&Number.isFinite(volume))total+=Math.abs(volume);
  }
  return total;
}
function wallPrice(point,prefix){
  let bestPrice=NaN;
  let bestVolume=-1;
  for(let level=1;level<=3;level++){
    const price=Number(point[`${prefix}${level}`]);
    const volume=Math.abs(Number(point[`${prefix}${level}v`]));
    if(Number.isFinite(price)&&Number.isFinite(volume)&&volume>bestVolume){
      bestVolume=volume;
      bestPrice=price;
    }
  }
  return bestPrice;
}
function bookSeries(data,product){
  data._bookSeries=data._bookSeries||{};
  if(data._bookSeries[product])return data._bookSeries[product];
  const activities=sortedActivities(data,product);
  const result={x:[],spread:[],bid1:[],ask1:[],bid3:[],ask3:[],imb1:[],imb3:[],micro:[],wallMid:[]};
  activities.forEach(point=>{
    const b1=Number(point.b1);
    const a1=Number(point.a1);
    const b1v=Math.abs(Number(point.b1v));
    const a1v=Math.abs(Number(point.a1v));
    const bid1=depthValue(point,"b",1);
    const ask1=depthValue(point,"a",1);
    const bid3=depthValue(point,"b",3);
    const ask3=depthValue(point,"a",3);
    const wallBid=wallPrice(point,"b");
    const wallAsk=wallPrice(point,"a");
    result.x.push(Number(point.ts));
    result.spread.push(Number.isFinite(a1)&&Number.isFinite(b1)?a1-b1:NaN);
    result.bid1.push(bid1);
    result.ask1.push(ask1);
    result.bid3.push(bid3);
    result.ask3.push(ask3);
    result.imb1.push(bid1+ask1?((bid1-ask1)/(bid1+ask1)):NaN);
    result.imb3.push(bid3+ask3?((bid3-ask3)/(bid3+ask3)):NaN);
    result.micro.push(Number.isFinite(b1)&&Number.isFinite(a1)&&b1v+a1v?((a1*b1v+b1*a1v)/(b1v+a1v)):NaN);
    result.wallMid.push(Number.isFinite(wallBid)&&Number.isFinite(wallAsk)?(wallBid+wallAsk)/2:NaN);
  });
  data._bookSeries[product]=result;
  return result;
}
function drawdownSeries(values){
  let peak=-Infinity;
  return values.map(value=>{
    const n=Number(value);
    if(!Number.isFinite(n))return NaN;
    peak=Math.max(peak,n);
    return n-peak;
  });
}
function rollingDeltaSeries(x,y,window){
  const outX=[],outY=[];
  for(let i=0;i<y.length;i++){
    const prev=Math.max(0,i-window);
    const value=Number(y[i])-Number(y[prev]);
    if(Number.isFinite(value)){
      outX.push(x[i]);
      outY.push(value);
    }
  }
  return {x:outX,y:outY};
}
function rollingStdSeries(x,y,window){
  const diffs=[];
  for(let i=1;i<y.length;i++)diffs.push(Number(y[i])-Number(y[i-1]));
  const outX=[],outY=[];
  for(let i=0;i<diffs.length;i++){
    const slice=diffs.slice(Math.max(0,i-window+1),i+1);
    const value=stdev(slice);
    if(Number.isFinite(value)){
      outX.push(x[i+1]);
      outY.push(value);
    }
  }
  return {x:outX,y:outY};
}
function productPnlTotalForData(data,product,dayMeta=null){
  if(dayMeta?.pnl_by_product&&product in dayMeta.pnl_by_product)return Number(dayMeta.pnl_by_product[product]||0);
  const activities=data.activities[product]||[];
  const last=[...activities].reverse().find(point=>Number.isFinite(Number(point.pnl)));
  return last?Number(last.pnl):0;
}
function topProductsByAbsPnl(data,prods,dayMeta,limit=12){
  return [...prods]
    .sort((a,b)=>Math.abs(productPnlTotalForData(data,b,dayMeta))-Math.abs(productPnlTotalForData(data,a,dayMeta)))
    .slice(0,limit);
}
function aggregatePnlSeries(data,prods){
  const trace=totalPnlTraceForData(data,prods,"TOTAL");
  return {x:trace.x,y:trace.y};
}
function bucketTradeFlow(data,prods,buckets=80){
  const trades=prods.flatMap(product=>(data.trades[product]||[]).map(trade=>Object.assign({product},trade)));
  const timestamps=trades.map(trade=>Number(trade.ts)).filter(Number.isFinite);
  if(!timestamps.length)return {x:[],bot:[],market:[],qty:[]};
  const bounds=minMax(timestamps);
  const min=bounds.lo,max=bounds.hi;
  const step=Math.max(1,(max-min+1)/buckets);
  const x=Array.from({length:buckets},(_,i)=>Math.round(min+i*step));
  const bot=Array(buckets).fill(0),market=Array(buckets).fill(0),qty=Array(buckets).fill(0);
  trades.forEach(trade=>{
    const idx=Math.min(buckets-1,Math.max(0,Math.floor((Number(trade.ts)-min)/step)));
    const isBot=trade.buyer==="SUBMISSION"||trade.seller==="SUBMISSION";
    if(isBot)bot[idx]+=1;
    else market[idx]+=1;
    qty[idx]+=Math.abs(Number(trade.qty||0));
  });
  return {x,bot,market,qty};
}
function participantRows(data,prods){
  const map=new Map();
  prods.forEach(product=>(data.trades[product]||[]).forEach(trade=>{
    [["buyer",1],["seller",-1]].forEach(([field,side])=>{
      const name=String(trade[field]||"").trim();
      if(!name)return;
      const row=map.get(name)||{name,buyTrades:0,sellTrades:0,buyQty:0,sellQty:0,notional:0,products:new Set()};
      const qty=Math.abs(Number(trade.qty||0));
      if(side>0){row.buyTrades+=1;row.buyQty+=qty;}
      else{row.sellTrades+=1;row.sellQty+=qty;}
      row.notional+=Math.abs(Number(trade.price||0)*qty);
      row.products.add(product);
      map.set(name,row);
    });
  }));
  return [...map.values()]
    .map(row=>Object.assign({},row,{products:[...row.products]}))
    .sort((a,b)=>b.notional-a.notional);
}
function alignedSeries(data,product,key="mid"){
  const map=new Map();
  (data.activities[product]||[]).forEach(point=>{
    const value=Number(point[key]);
    if(Number.isFinite(value))map.set(Number(point.ts),value);
  });
  return map;
}
function alignedVectors(data,a,b,key="mid"){
  const left=alignedSeries(data,a,key);
  const right=alignedSeries(data,b,key);
  const x=[],y=[],ts=[];
  left.forEach((value,time)=>{
    if(right.has(time)){
      x.push(value);
      y.push(right.get(time));
      ts.push(time);
    }
  });
  return {x,y,ts};
}
function corr(xs,ys){
  if(xs.length<3||ys.length<3||xs.length!==ys.length)return NaN;
  const mx=avg(xs),my=avg(ys);
  let num=0,dx=0,dy=0;
  xs.forEach((x,i)=>{
    const ax=x-mx,ay=ys[i]-my;
    num+=ax*ay;dx+=ax*ax;dy+=ay*ay;
  });
  return dx&&dy?num/Math.sqrt(dx*dy):NaN;
}
function regression(xs,ys){
  const mx=avg(xs),my=avg(ys);
  let num=0,den=0;
  xs.forEach((x,i)=>{num+=(x-mx)*(ys[i]-my);den+=(x-mx)**2;});
  const beta=den?num/den:0;
  return {alpha:my-beta*mx,beta};
}
function zScoreSeries(values,window=120){
  return values.map((value,index)=>{
    const slice=values.slice(Math.max(0,index-window+1),index+1).map(Number).filter(Number.isFinite);
    const sd=stdev(slice);
    const mean=avg(slice);
    return Number.isFinite(sd)&&sd>0?(Number(value)-mean)/sd:0;
  });
}
function botSide(trade){
  if(trade.buyer==="SUBMISSION")return "BUY";
  if(trade.seller==="SUBMISSION")return "SELL";
  return "MKT";
}
function productStats(data,product,dayMeta){
  const activities=data.activities[product]||[];
  const trades=data.trades[product]||[];
  const bot=trades.filter(trade=>trade.buyer==="SUBMISSION"||trade.seller==="SUBMISSION");
  const buys=bot.filter(trade=>trade.buyer==="SUBMISSION");
  const sells=bot.filter(trade=>trade.seller==="SUBMISSION");
  const pnlValues=activities.map(point=>Number(point.pnl)).filter(Number.isFinite);
  const spreads=activities.map(point=>Number(point.a1)-Number(point.b1)).filter(Number.isFinite);
  const finalPnl=Number(dayMeta?.pnl_by_product?.[product]??lastFinite(pnlValues));
  const buyQty=buys.reduce((sum,trade)=>sum+Number(trade.qty||0),0);
  const sellQty=sells.reduce((sum,trade)=>sum+Number(trade.qty||0),0);
  const grossNotional=bot.reduce((sum,trade)=>sum+Math.abs(Number(trade.qty||0)*Number(trade.price||0)),0);
  const fills=getTradeAnalytics(data,product);
  const inventory=inventorySeries(data,product);
  const book=bookSeries(data,product);
  const avgEdge=avg(fills.map(row=>row.edge));
  const avgMarkout10=avg(fills.map(row=>row.markouts[10]));
  const avgSpreadCapture=avg(fills.map(row=>row.spreadCapture));
  const takeRate=fills.length?fills.filter(row=>row.crossed).length/fills.length:NaN;
  const maxAbsPosition=inventory.pos.length?Math.max(...inventory.pos.map(value=>Math.abs(Number(value)||0))):0;
  const maxExposure=inventory.exposure.length?Math.max(...inventory.exposure.map(value=>Number(value)||0)):0;
  const avgDepth=avg(book.bid3.map((value,index)=>Number(value)+Number(book.ask3[index])));
  const avgImbalance=avg(book.imb3.map(value=>Math.abs(Number(value))));
  return {
    product,
    finalPnl,
    points:activities.length,
    botTrades:bot.length,
    marketTrades:trades.length-bot.length,
    buys:buys.length,
    sells:sells.length,
    netQty:buyQty-sellQty,
    grossNotional,
    avgSpread:avg(spreads),
    drawdown:maxDrawdown(pnlValues),
    pnlMin:pnlValues.length?Math.min(...pnlValues):0,
    pnlMax:pnlValues.length?Math.max(...pnlValues):0,
    avgEdge,
    avgMarkout10,
    avgSpreadCapture,
    takeRate,
    maxAbsPosition,
    maxExposure,
    avgDepth,
    avgImbalance,
  };
}
function totalPnlTraceForData(data,prods,name="TOTAL",color=cssVar("--ink"),opts={}){
  const tsMap={};
  prods.forEach(product=>(data.activities[product]||[]).forEach(point=>{
    tsMap[point.ts]=(tsMap[point.ts]||0)+(Number(point.pnl)||0);
  }));
  const tss=Object.keys(tsMap).map(Number).sort((a,b)=>a-b);
  let y=tss.map(ts=>tsMap[ts]);
  if(opts.normalize&&y.length){
    const start=y[0];
    y=y.map(value=>value-start);
  }
  return {x:tss,y,name,type:"scatter",mode:"lines",line:{width:opts.width||2.4,color,dash:opts.dash}};
}
function totalPnlTrace(prods){
  return totalPnlTraceForData(currentData,prods,"TOTAL",cssVar("--accent-2"),{width:3});
}
function productPnlTotal(product){
  const activities=currentData.activities[product]||[];
  const last=[...activities].reverse().find(point=>Number.isFinite(Number(point.pnl)));
  return last?Number(last.pnl):0;
}
function makePnlChart(parent,prods,opts={}){
  const el=makeChartWrap(parent,"Cumulative PnL over time",300);
  const traces=[];
  if(opts.totalOnly){
    traces.push(totalPnlTrace(prods));
  }else{
    let shown=prods;
    if(opts.topProducts){
      shown=[...prods].sort((a,b)=>Math.abs(productPnlTotal(b))-Math.abs(productPnlTotal(a))).slice(0,opts.topProducts);
    }
    shown.forEach((product,index)=>{
      const activities=currentData.activities[product]||[];
      traces.push({x:activities.map(point=>point.ts),y:activities.map(point=>point.pnl),name:product,type:"scatter",mode:"lines",line:{width:1.6,color:COLORS[index%COLORS.length]}});
    });
    if(prods.length>1){
      const total=totalPnlTrace(prods);
      total.line.dash="dash";
      traces.push(total);
    }
  }
  plot(el,traces,{yaxis:{title:"PnL"}},{includeZeroY:true});
}
function makeProductPnlBar(parent,prods){
  const el=makeChartWrap(parent,"Final PnL by product",330);
  const dayMeta=currentRun.days.find(day=>day.dir===currentData._dir);
  const pnlByProduct=dayMeta?.pnl_by_product||{};
  const rows=prods.map(product=>[product,Number(pnlByProduct[product]??productPnlTotal(product))]).sort((a,b)=>b[1]-a[1]);
  const traces=[{x:rows.map(row=>row[0]),y:rows.map(row=>row[1]),type:"bar",marker:{color:rows.map(row=>row[1]>=0?cssVar("--good"):cssVar("--bad"))},name:"Final PnL"}];
  plot(el,traces,{yaxis:{title:"Final PnL"}},{includeZeroY:true});
}
function makeSingleProductCharts(parent,product){
  const el=makeChartWrap(parent,`${product} - Price and Trades`,340);
  const activities=currentData.activities[product]||[];
  const trades=currentData.trades[product]||[];
  const traces=[{x:activities.map(point=>point.ts),y:activities.map(point=>point.mid),name:"Mid",type:"scatter",mode:"lines",line:{width:1.8,color:COLORS[0]}}];
  if(showSpread){
    traces.push({x:activities.map(point=>point.ts),y:activities.map(point=>point.b1),name:"Bid 1",type:"scatter",mode:"lines",line:{width:1,color:cssVar("--good"),dash:"dot"}});
    traces.push({x:activities.map(point=>point.ts),y:activities.map(point=>point.a1),name:"Ask 1",type:"scatter",mode:"lines",line:{width:1,color:cssVar("--bad"),dash:"dot"}});
  }
  if(showMarketTrades&&trades.length){
    const market=trades.filter(trade=>trade.buyer!=="SUBMISSION"&&trade.seller!=="SUBMISSION");
    if(market.length)traces.push({x:market.map(trade=>trade.ts),y:market.map(trade=>trade.price),name:"Market Trades",type:"scatter",mode:"markers",marker:{symbol:"circle",color:cssVar("--faint"),size:5,opacity:.42}});
  }
  if(showBotData&&trades.length){
    const fills=getTradeAnalytics(currentData,product);
    const buys=fills.filter(trade=>trade.side==="BUY");
    const sells=fills.filter(trade=>trade.side==="SELL");
    if(buys.length)traces.push({
      x:buys.map(trade=>trade.ts),
      y:buys.map(trade=>trade.price),
      name:"Bot Buys",
      type:"scatter",
      mode:"markers",
      text:buys.map(trade=>`edge ${signed(trade.edge,2)} | q ${fmt(trade.qty)}`),
      marker:{symbol:"triangle-up",color:buys.map(trade=>trade.edge>=0?cssVar("--good"):cssVar("--bad")),size:buys.map(trade=>Math.min(14,6+Math.sqrt(Math.abs(trade.qty||0))))},
    });
    if(sells.length)traces.push({
      x:sells.map(trade=>trade.ts),
      y:sells.map(trade=>trade.price),
      name:"Bot Sells",
      type:"scatter",
      mode:"markers",
      text:sells.map(trade=>`edge ${signed(trade.edge,2)} | q ${fmt(trade.qty)}`),
      marker:{symbol:"triangle-down",color:sells.map(trade=>trade.edge>=0?cssVar("--good"):cssVar("--bad")),size:sells.map(trade=>Math.min(14,6+Math.sqrt(Math.abs(trade.qty||0))))},
    });
  }
  plot(el,traces);
  const pnlEl=makeChartWrap(parent,`${product} - PnL`,230);
  plot(pnlEl,[{x:activities.map(point=>point.ts),y:activities.map(point=>point.pnl),type:"scatter",mode:"lines",fill:"tozeroy",line:{color:COLORS[0],width:1.7},fillcolor:"rgba(96,165,250,.14)",name:"PnL"}],{yaxis:{title:"PnL"}},{includeZeroY:true});
}
function makeRiskAnalytics(parent,prods,prodSel){
  const dayMeta=currentDayMeta();
  const series=aggregatePnlSeries(currentData,prods);
  if(!series.x.length)return;

  const dd=drawdownSeries(series.y);
  const ddEl=makeChartWrap(parent,"Underwater Drawdown",240);
  plot(ddEl,[{
    x:series.x,
    y:dd,
    name:"Drawdown",
    type:"scatter",
    mode:"lines",
    fill:"tozeroy",
    line:{color:cssVar("--bad"),width:1.8},
    fillcolor:"rgba(251,113,133,.18)",
  }],{yaxis:{title:"PnL from peak"}},{includeZeroY:true});

  const window=Math.max(8,Math.round(series.y.length/85));
  const delta=rollingDeltaSeries(series.x,series.y,window);
  const vol=rollingStdSeries(series.x,series.y,window);
  const rollEl=makeChartWrap(parent,`Rolling PnL Delta and Volatility (${window} ticks)`,260);
  plot(rollEl,[
    {x:delta.x,y:delta.y,name:"Rolling PnL delta",type:"scatter",mode:"lines",line:{color:cssVar("--accent-2"),width:1.8}},
    {x:vol.x,y:vol.y,name:"Rolling volatility",type:"scatter",mode:"lines",yaxis:"y2",line:{color:cssVar("--warn"),width:1.4,dash:"dot"}},
  ],{
    yaxis:{title:"Delta"},
    yaxis2:{title:"Vol",overlaying:"y",side:"right",gridcolor:"rgba(0,0,0,0)",zeroline:false},
  },{autoY:true});

  if(prods.length>1){
    const rows=prods.map(product=>productStats(currentData,product,dayMeta));
    const scatter=makeChartWrap(parent,"Product Risk / Return Map",300);
    plot(scatter,[{
      x:rows.map(row=>row.drawdown),
      y:rows.map(row=>row.finalPnl),
      text:rows.map(row=>`${row.product}<br>PnL ${signed(row.finalPnl)}<br>DD ${fmt(row.drawdown)}<br>Trades ${fmt(row.botTrades)}`),
      type:"scatter",
      mode:"markers",
      marker:{
        size:rows.map(row=>Math.min(28,8+Math.sqrt(row.botTrades||0))),
        color:rows.map(row=>row.finalPnl),
        colorscale:[[0,cssVar("--bad")],[.5,cssVar("--faint")],[1,cssVar("--good")]],
        showscale:false,
        line:{width:1,color:cssVar("--line-strong")},
      },
      name:"Products",
    }],{xaxis:{title:"Max Drawdown"},yaxis:{title:"Final PnL"}},{includeZeroY:true});
  }
}
function makeExecutionAnalytics(parent,prods,prodSel){
  const dayMeta=currentDayMeta();
  const stats=prods.map(product=>productStats(currentData,product,dayMeta)).sort((a,b)=>b.finalPnl-a.finalPnl);
  if(prods.length===1){
    const product=prods[0];
    const inventory=inventorySeries(currentData,product);
    const fills=getTradeAnalytics(currentData,product);
    const posEl=makeChartWrap(parent,`${product} - Position and Exposure`,270);
    plot(posEl,[
      {x:inventory.x,y:inventory.pos,name:"Position",type:"scatter",mode:"lines",line:{color:cssVar("--accent-2"),width:1.8}},
      {x:inventory.x,y:inventory.exposure,name:"Abs exposure",type:"scatter",mode:"lines",yaxis:"y2",line:{color:cssVar("--warn"),width:1.4,dash:"dot"}},
    ],{
      yaxis:{title:"Position"},
      yaxis2:{title:"Exposure",overlaying:"y",side:"right",gridcolor:"rgba(0,0,0,0)",zeroline:false},
    },{autoY:true});

    if(fills.length){
      const edgeEl=makeChartWrap(parent,`${product} - Fill Edge vs Mid`,260);
      plot(edgeEl,[{
        x:fills.map(row=>row.ts),
        y:fills.map(row=>row.edge),
        text:fills.map(row=>`${row.side} q${fmt(row.qty)} @ ${fmt(row.price,2)}<br>mid ${fmt(row.mid,2)} | spread ${fmt(row.spread,2)}<br>capture ${pct(row.spreadCapture)}`),
        name:"Signed edge",
        type:"scatter",
        mode:"markers",
        marker:{symbol:fills.map(row=>row.side==="BUY"?"triangle-up":"triangle-down"),color:fills.map(row=>row.edge>=0?cssVar("--good"):cssVar("--bad")),size:fills.map(row=>Math.min(14,6+Math.sqrt(row.qty||0)))},
      }],{yaxis:{title:"Signed edge"}},{includeZeroY:true});

      const horizons=[1,5,10,50];
      const markoutEl=makeChartWrap(parent,`${product} - Average Markout`,240);
      plot(markoutEl,[{
        x:horizons.map(h=>`${h}t`),
        y:horizons.map(h=>avg(fills.map(row=>row.markouts[h]))),
        type:"bar",
        marker:{color:horizons.map(h=>avg(fills.map(row=>row.markouts[h]))>=0?cssVar("--good"):cssVar("--bad"))},
        name:"Avg markout",
      }],{yaxis:{title:"Signed markout"}},{includeZeroY:true});
    }
  }else{
    const exposureMap={};
    prods.forEach(product=>{
      const inventory=inventorySeries(currentData,product);
      inventory.x.forEach((ts,index)=>{
        exposureMap[ts]=(exposureMap[ts]||0)+(Number(inventory.exposure[index])||0);
      });
    });
    const exposureTs=Object.keys(exposureMap).map(Number).sort((a,b)=>a-b);
    if(exposureTs.length){
      const exposureEl=makeChartWrap(parent,"Aggregate Notional Exposure",240);
      plot(exposureEl,[{
        x:exposureTs,
        y:exposureTs.map(ts=>exposureMap[ts]),
        name:"Gross exposure",
        type:"scatter",
        mode:"lines",
        fill:"tozeroy",
        line:{color:cssVar("--warn"),width:1.6},
        fillcolor:"rgba(251,191,36,.14)",
      }],{yaxis:{title:"Abs position * mid"}},{includeZeroY:true});
    }
    const edgeRows=stats.filter(row=>row.botTrades);
    if(edgeRows.length){
      const edgeEl=makeChartWrap(parent,"Execution Edge by Product",330);
      plot(edgeEl,[
        {x:edgeRows.map(row=>row.product),y:edgeRows.map(row=>row.avgEdge),type:"bar",name:"Fill edge",marker:{color:edgeRows.map(row=>row.avgEdge>=0?cssVar("--good"):cssVar("--bad"))}},
        {x:edgeRows.map(row=>row.product),y:edgeRows.map(row=>row.avgMarkout10),type:"bar",name:"10-tick markout",marker:{color:cssVar("--accent-2")}},
      ],{barmode:"group",yaxis:{title:"Signed edge"}},{includeZeroY:true});
    }
  }

  const flow=bucketTradeFlow(currentData,prods);
  if(flow.x.length){
    const flowEl=makeChartWrap(parent,"Trade Burst Timeline",240);
    const traces=[
      {x:flow.x,y:flow.bot,name:"Bot fills",type:"bar",marker:{color:cssVar("--accent-2")}},
    ];
    if(showMarketTrades)traces.push({x:flow.x,y:flow.market,name:"Market prints",type:"bar",marker:{color:cssVar("--faint")}});
    plot(flowEl,traces,{barmode:"stack",yaxis:{title:"Trades"}},{includeZeroY:true});
  }
  makeExecutionTable(parent,stats);
  makeParticipantTable(parent,prods);
}
function makeBookAnalytics(parent,prods,prodSel){
  const dayMeta=currentDayMeta();
  if(prods.length===1){
    const product=prods[0];
    const book=bookSeries(currentData,product);
    if(!book.x.length)return;
    const depthEl=makeChartWrap(parent,`${product} - Book Depth`,270);
    plot(depthEl,[
      {x:book.x,y:book.bid1,name:"Bid L1",type:"scatter",mode:"lines",line:{color:cssVar("--good"),width:1.4}},
      {x:book.x,y:book.ask1.map(value=>-value),name:"Ask L1",type:"scatter",mode:"lines",line:{color:cssVar("--bad"),width:1.4}},
      {x:book.x,y:book.bid3,name:"Bid L1-L3",type:"scatter",mode:"lines",line:{color:cssVar("--good"),width:1,dash:"dot"}},
      {x:book.x,y:book.ask3.map(value=>-value),name:"Ask L1-L3",type:"scatter",mode:"lines",line:{color:cssVar("--bad"),width:1,dash:"dot"}},
    ],{yaxis:{title:"Depth (ask negative)" }},{includeZeroY:true});

    const microEl=makeChartWrap(parent,`${product} - Microprice and Wall Mid`,270);
    const activities=sortedActivities(currentData,product);
    plot(microEl,[
      {x:book.x,y:activities.map(point=>point.mid),name:"Mid",type:"scatter",mode:"lines",line:{color:COLORS[0],width:1.5}},
      {x:book.x,y:book.micro,name:"Microprice",type:"scatter",mode:"lines",line:{color:cssVar("--accent"),width:1.5}},
      {x:book.x,y:book.wallMid,name:"Wall mid",type:"scatter",mode:"lines",line:{color:cssVar("--warn"),width:1.2,dash:"dot"}},
    ],{yaxis:{title:"Price"}});

    const imbEl=makeChartWrap(parent,`${product} - Imbalance and Spread`,250);
    plot(imbEl,[
      {x:book.x,y:book.imb1,name:"L1 imbalance",type:"scatter",mode:"lines",line:{color:cssVar("--accent-2"),width:1.5}},
      {x:book.x,y:book.imb3,name:"L1-L3 imbalance",type:"scatter",mode:"lines",line:{color:cssVar("--accent"),width:1.2,dash:"dot"}},
      {x:book.x,y:book.spread,name:"Spread",type:"scatter",mode:"lines",yaxis:"y2",line:{color:cssVar("--warn"),width:1}},
    ],{
      yaxis:{title:"Imbalance",range:[-1.05,1.05],autorange:false},
      yaxis2:{title:"Spread",overlaying:"y",side:"right",gridcolor:"rgba(0,0,0,0)",zeroline:false},
    },{autoY:true});

    const hist=makeChartWrap(parent,`${product} - Spread Distribution`,220);
    plot(hist,[{x:book.spread.filter(Number.isFinite),type:"histogram",marker:{color:cssVar("--accent-2")},name:"Spread"}],{xaxis:{title:"Spread"},yaxis:{title:"Ticks"}},{autoY:true});
  }else{
    const rows=prods.map(product=>productStats(currentData,product,dayMeta)).sort((a,b)=>b.finalPnl-a.finalPnl);
    const scatter=makeChartWrap(parent,"Book Quality Map",310);
    plot(scatter,[{
      x:rows.map(row=>row.avgSpread),
      y:rows.map(row=>row.finalPnl),
      text:rows.map(row=>`${row.product}<br>spread ${fmt(row.avgSpread,2)}<br>depth ${fmt(row.avgDepth,1)}<br>imbalance ${fmt(row.avgImbalance,3)}`),
      type:"scatter",
      mode:"markers",
      marker:{
        size:rows.map(row=>Math.min(26,8+Math.sqrt(Math.max(0,row.avgDepth||0)))),
        color:rows.map(row=>row.avgImbalance),
        colorscale:"Viridis",
        showscale:true,
        colorbar:{title:"Abs Imb"},
        line:{width:1,color:cssVar("--line-strong")},
      },
      name:"Products",
    }],{xaxis:{title:"Avg Spread"},yaxis:{title:"Final PnL"}},{includeZeroY:true});
    makeBookTable(parent,rows);
  }
}
function makeRelationAnalytics(parent,prods,prodSel){
  const dayMeta=currentDayMeta();
  const top=topProductsByAbsPnl(currentData,prods,dayMeta,12);
  if(top.length<2)return;
  makeContributionHeatmap(parent,top);
  const matrix=correlationMatrix(currentData,top);
  if(matrix.products.length>1){
    const corrEl=makeChartWrap(parent,"Cross-Product Return Correlation",360);
    plot(corrEl,[{
      x:matrix.products,
      y:matrix.products,
      z:matrix.values,
      type:"heatmap",
      colorscale:[[0,cssVar("--bad")],[.5,cssVar("--plot-2")],[1,cssVar("--good")]],
      zmin:-1,
      zmax:1,
      colorbar:{title:"Corr"},
    }],{margin:{t:20,b:120,l:160,r:24}},{autoY:true});
    const pair=bestCorrelationPair(matrix);
    if(pair)makePairDiagnostics(parent,pair.a,pair.b);
  }
}
function makeContributionHeatmap(parent,prods){
  const allTs=prods.flatMap(product=>(currentData.activities[product]||[]).map(point=>Number(point.ts))).filter(Number.isFinite);
  if(!allTs.length)return;
  const bounds=minMax(allTs);
  const min=bounds.lo,max=bounds.hi;
  const buckets=120;
  const times=Array.from({length:buckets},(_,i)=>Math.round(min+(max-min)*i/(buckets-1||1)));
  const z=prods.map(product=>{
    const activities=sortedActivities(currentData,product);
    let idx=0;
    const start=Number(activities[0]?.pnl||0);
    return times.map(ts=>{
      while(idx<activities.length-1&&Number(activities[idx+1].ts)<=ts)idx+=1;
      return Number(activities[idx]?.pnl||0)-start;
    });
  });
  const el=makeChartWrap(parent,"Product Contribution Heatmap",390);
  plot(el,[{
    x:times,
    y:prods,
    z,
    type:"heatmap",
    colorscale:[[0,cssVar("--bad")],[.5,cssVar("--plot-2")],[1,cssVar("--good")]],
    colorbar:{title:"PnL"},
  }],{margin:{t:20,b:42,l:190,r:24}},{autoY:true});
}
function returns(values){
  const out=[];
  for(let i=1;i<values.length;i++){
    const value=Number(values[i])-Number(values[i-1]);
    if(Number.isFinite(value))out.push(value);
  }
  return out;
}
function correlationMatrix(data,products){
  const values=products.map(a=>products.map(b=>{
    if(a===b)return 1;
    const aligned=alignedVectors(data,a,b,"mid");
    return corr(returns(aligned.x),returns(aligned.y));
  }));
  return {products,values};
}
function bestCorrelationPair(matrix){
  let best=null;
  matrix.products.forEach((a,i)=>matrix.products.forEach((b,j)=>{
    if(j<=i)return;
    const value=Number(matrix.values[i][j]);
    if(!Number.isFinite(value))return;
    if(!best||Math.abs(value)>Math.abs(best.corr))best={a,b,corr:value};
  }));
  return best;
}
function makePairDiagnostics(parent,a,b){
  const aligned=alignedVectors(currentData,a,b,"mid");
  if(aligned.x.length<4)return;
  const reg=regression(aligned.x,aligned.y);
  const fitted=aligned.x.map(x=>reg.alpha+reg.beta*x);
  const residual=aligned.y.map((y,i)=>y-fitted[i]);
  const z=zScoreSeries(residual,120);
  const scatter=makeChartWrap(parent,`Pair Regression: ${a} / ${b}`,310);
  plot(scatter,[
    {x:aligned.x,y:aligned.y,type:"scatter",mode:"markers",name:"Ticks",marker:{size:4,color:cssVar("--accent-2"),opacity:.45}},
    {x:aligned.x,y:fitted,type:"scatter",mode:"lines",name:`fit beta ${fmt(reg.beta,3)}`,line:{color:cssVar("--warn"),width:2}},
  ],{xaxis:{title:a},yaxis:{title:b}},{autoY:true});
  const zEl=makeChartWrap(parent,`Residual Z-Score: ${a} / ${b}`,240);
  plot(zEl,[{
    x:aligned.ts,
    y:z,
    name:"Spread z-score",
    type:"scatter",
    mode:"lines",
    line:{color:cssVar("--accent-2"),width:1.5},
  }],{yaxis:{title:"Z",range:[-4,4],autorange:false}},{includeZeroY:true});
}
function makeExecutionTable(parent,rows){
  const scroller=makeTableWrap(parent,"Execution Diagnostics");
  scroller.innerHTML=`<table class="data-table">
    <thead><tr><th>Product</th><th>Fills</th><th>Avg Edge</th><th>10t Markout</th><th>Spread Capture</th><th>Cross Rate</th><th>Max Pos</th><th>Max Exposure</th></tr></thead>
    <tbody>${rows.map(row=>`<tr>
      <td title="${esc(row.product)}">${esc(row.product)}</td>
      <td>${showBotData?fmt(row.botTrades):"hidden"}</td>
      <td class="${pnlClass(row.avgEdge)}">${showBotData&&Number.isFinite(row.avgEdge)?signed(row.avgEdge,2):"-"}</td>
      <td class="${pnlClass(row.avgMarkout10)}">${showBotData&&Number.isFinite(row.avgMarkout10)?signed(row.avgMarkout10,2):"-"}</td>
      <td>${showBotData?pct(row.avgSpreadCapture):"hidden"}</td>
      <td>${showBotData?pct(row.takeRate):"hidden"}</td>
      <td>${showBotData?fmt(row.maxAbsPosition):"hidden"}</td>
      <td>${showBotData?fmt(row.maxExposure):"hidden"}</td>
    </tr>`).join("")}</tbody>
  </table>`;
}
function makeBookTable(parent,rows){
  const scroller=makeTableWrap(parent,"Order Book Diagnostics");
  scroller.innerHTML=`<table class="data-table">
    <thead><tr><th>Product</th><th>PnL</th><th>Avg Spread</th><th>Avg L1-L3 Depth</th><th>Avg Abs Imbalance</th><th>Avg Edge</th><th>Ticks</th></tr></thead>
    <tbody>${rows.map(row=>`<tr>
      <td title="${esc(row.product)}">${esc(row.product)}</td>
      <td class="${pnlClass(row.finalPnl)}">${signed(row.finalPnl)}</td>
      <td>${Number.isFinite(row.avgSpread)?fmt(row.avgSpread,2):"-"}</td>
      <td>${Number.isFinite(row.avgDepth)?fmt(row.avgDepth,1):"-"}</td>
      <td>${Number.isFinite(row.avgImbalance)?fmt(row.avgImbalance,3):"-"}</td>
      <td class="${pnlClass(row.avgEdge)}">${Number.isFinite(row.avgEdge)?signed(row.avgEdge,2):"-"}</td>
      <td>${fmt(row.points)}</td>
    </tr>`).join("")}</tbody>
  </table>`;
}
function makeParticipantTable(parent,prods){
  const rows=participantRows(currentData,prods).slice(0,24);
  if(!rows.length)return;
  const scroller=makeTableWrap(parent,"Participant / Bot Footprint");
  scroller.innerHTML=`<table class="data-table">
    <thead><tr><th>Participant</th><th>Buy Trades</th><th>Sell Trades</th><th>Buy Qty</th><th>Sell Qty</th><th>Net Qty</th><th>Notional</th><th>Products</th></tr></thead>
    <tbody>${rows.map(row=>`<tr>
      <td title="${esc(row.name)}">${esc(row.name)}</td>
      <td>${fmt(row.buyTrades)}</td>
      <td>${fmt(row.sellTrades)}</td>
      <td>${fmt(row.buyQty)}</td>
      <td>${fmt(row.sellQty)}</td>
      <td class="${pnlClass(row.buyQty-row.sellQty)}">${signed(row.buyQty-row.sellQty)}</td>
      <td>${fmt(row.notional)}</td>
      <td title="${esc(row.products.join(", "))}">${fmt(row.products.length)}</td>
    </tr>`).join("")}</tbody>
  </table>`;
}
function makeProductTable(parent,prods){
  const dayMeta=currentDayMeta();
  const rows=prods.map(product=>productStats(currentData,product,dayMeta)).sort((a,b)=>b.finalPnl-a.finalPnl);
  const scroller=makeTableWrap(parent,"Product Diagnostics");
  scroller.innerHTML=`<table class="data-table">
    <thead><tr><th>Product</th><th>PnL</th><th>Drawdown</th><th>Bot Trades</th><th>Net Qty</th><th>Max Pos</th><th>Avg Edge</th><th>10t Markout</th><th>Spread Cap</th><th>Avg Spread</th><th>Depth</th><th>Ticks</th></tr></thead>
    <tbody>${rows.map(row=>`<tr>
      <td title="${esc(row.product)}">${esc(row.product)}</td>
      <td class="${pnlClass(row.finalPnl)}">${signed(row.finalPnl)}</td>
      <td>${fmt(row.drawdown)}</td>
      <td>${showBotData?fmt(row.botTrades):"hidden"}</td>
      <td>${showBotData?fmt(row.netQty):"hidden"}</td>
      <td>${showBotData?fmt(row.maxAbsPosition):"hidden"}</td>
      <td class="${pnlClass(row.avgEdge)}">${showBotData&&Number.isFinite(row.avgEdge)?signed(row.avgEdge,2):"-"}</td>
      <td class="${pnlClass(row.avgMarkout10)}">${showBotData&&Number.isFinite(row.avgMarkout10)?signed(row.avgMarkout10,2):"-"}</td>
      <td>${showBotData?pct(row.avgSpreadCapture):"hidden"}</td>
      <td>${Number.isFinite(row.avgSpread)?fmt(row.avgSpread,2):"-"}</td>
      <td>${Number.isFinite(row.avgDepth)?fmt(row.avgDepth,1):"-"}</td>
      <td>${fmt(row.points)}</td>
    </tr>`).join("")}</tbody>
  </table>`;
}
function makeTradeTape(parent,product){
  const trades=(currentData.trades[product]||[]).filter(trade=>trade.buyer==="SUBMISSION"||trade.seller==="SUBMISSION").slice(-80).reverse();
  const scroller=makeTableWrap(parent,`${product} - Bot Trade Tape`);
  if(!trades.length){
    scroller.innerHTML='<div class="summary-sub">No bot trades for this product.</div>';
    return;
  }
  scroller.innerHTML=`<table class="data-table">
    <thead><tr><th>Side</th><th>Timestamp</th><th>Price</th><th>Qty</th><th>Buyer</th><th>Seller</th><th>Notional</th></tr></thead>
    <tbody>${trades.map(trade=>{
      const side=botSide(trade);
      const notional=Number(trade.price||0)*Number(trade.qty||0);
      return `<tr>
        <td><span class="pill ${side==="BUY"?"pos":"neg"}">${side}</span></td>
        <td>${fmt(trade.ts)}</td>
        <td>${fmt(trade.price,2)}</td>
        <td>${fmt(trade.qty)}</td>
        <td>${esc(trade.buyer)}</td>
        <td>${esc(trade.seller)}</td>
        <td>${fmt(notional)}</td>
      </tr>`;
    }).join("")}</tbody>
  </table>`;
}
function toggleCompare(id){
  if(compareSet.has(id))compareSet.delete(id);
  else compareSet.add(id);
  updateCompareBar();
}
function updateCompareBar(){
  document.getElementById("cmp-label").textContent=compareSet.size?`${compareSet.size} selected for overlay`:"Select runs for overlay comparison";
  document.getElementById("cmp-btn").style.display=compareSet.size>1?"":"none";
  document.getElementById("cmp-clr").style.display=compareSet.size?"":"none";
  filterRuns();
  syncIcons();
}
function clearCompare(){
  compareSet.clear();
  updateCompareBar();
  if(activeMode==="compare")drawCharts();
}
async function openCompare(){
  const ids=[...compareSet];
  if(ids.length<2)return;
  activeMode="compare";
  const runs=ids.map(id=>allRuns.find(run=>run.id===id)).filter(Boolean);
  const selected=selectedProduct();
  const payloads=await Promise.all(runs.map(async (run,index)=>{
    const day=chooseDayForRun(run);
    const data=await fetchJson("/api/day?dir="+encodeURIComponent(day.dir));
    const products=(selected&&selected!=="[TOTAL]"&&selected!=="[ALL PRODUCTS]"&&data.products.includes(selected))?[selected]:data.products;
    const stats=products.map(product=>productStats(data,product,day));
    const finalPnl=stats.reduce((sum,row)=>sum+row.finalPnl,0);
    const drawdown=maxDrawdown(totalPnlTraceForData(data,products,runLabel(run.id),COLORS[index%COLORS.length]).y);
    const botTrades=stats.reduce((sum,row)=>sum+row.botTrades,0);
    const netQty=stats.reduce((sum,row)=>sum+row.netQty,0);
    return {run,day,data,products,stats,finalPnl,drawdown,botTrades,netQty,color:COLORS[index%COLORS.length]};
  }));
  const charts=document.getElementById("charts");
  charts.innerHTML="";
  makeCompareSummary(charts,payloads,selected);
  const overlay=makeChartWrap(charts,"Overlayed Cumulative PnL",380);
  plot(overlay,payloads.map(payload=>totalPnlTraceForData(payload.data,payload.products,runLabel(payload.run.id),payload.color,{width:2.6})),{yaxis:{title:"PnL"}},{includeZeroY:true});
  const finals=makeChartWrap(charts,"Final PnL by Selected Run",260);
  plot(finals,[{
    x:payloads.map(payload=>runLabel(payload.run.id)),
    y:payloads.map(payload=>payload.finalPnl),
    type:"bar",
    marker:{color:payloads.map(payload=>payload.finalPnl>=0?cssVar("--good"):cssVar("--bad"))},
    name:"Final PnL",
  }],{yaxis:{title:"Final PnL"}},{includeZeroY:true});
  makeCompareProductBreakdown(charts,payloads);
  if(showRiskAnalytics)makeCompareRiskAnalytics(charts,payloads);
  if(showExecutionAnalytics)makeCompareExecutionAnalytics(charts,payloads);
  if(showRelationAnalytics)makeCompareDeltaWaterfall(charts,payloads);
  makeCompareTable(charts,payloads);
  document.getElementById("toolbar-pnl").textContent=`Overlay comparison | ${payloads.length} runs | ${selected}`;
  document.getElementById("toolbar-pnl").style.color=cssVar("--accent-2");
  syncIcons();
  setTimeout(resizePlots,0);
  setTimeout(resizePlots,250);
}
function makeCompareSummary(parent,payloads,selected){
  const ranked=[...payloads].sort((a,b)=>b.finalPnl-a.finalPnl);
  const best=ranked[0];
  const worst=ranked[ranked.length-1];
  const spread=(best?.finalPnl??0)-(worst?.finalPnl??0);
  const targetDay=selectedCompareDayKey()||"best available day";
  const grid=document.createElement("div");
  grid.className="summary-grid";
  const cards=[
    ["Mode","Overlay",`${payloads.length} selected runs`],
    ["View",selected==="[ALL PRODUCTS]"?"Total overlay":selected,`Day ${targetDay.replace("day_","")}`],
    ["Best",best?`${runLabel(best.run.id)} ${signed(best.finalPnl)}`:"-","Highest final PnL"],
    ["Worst",worst?`${runLabel(worst.run.id)} ${signed(worst.finalPnl)}`:"-","Lowest final PnL"],
    ["Spread",fmt(spread), "Best minus worst"],
    ["Bot Trades",showBotData?fmt(payloads.reduce((sum,p)=>sum+p.botTrades,0)):"Hidden","Across selected runs"],
  ];
  grid.innerHTML=cards.map(([label,value,sub])=>`<div class="summary-card"><div class="summary-label">${esc(label)}</div><div class="summary-value" title="${esc(value)}">${esc(value)}</div><div class="summary-sub">${esc(sub)}</div></div>`).join("");
  parent.appendChild(grid);
}
function makeCompareProductBreakdown(parent,payloads){
  const products=[...new Set(payloads.flatMap(payload=>payload.products))].sort();
  const ranked=products
    .map(product=>[product,Math.max(...payloads.map(payload=>Math.abs(Number(payload.day.pnl_by_product?.[product]??0))))])
    .sort((a,b)=>b[1]-a[1])
    .slice(0,24)
    .map(row=>row[0]);
  if(!ranked.length)return;
  const el=makeChartWrap(parent,"Per-Product PnL Breakdown",360);
  plot(el,payloads.map(payload=>({
    x:ranked,
    y:ranked.map(product=>Number(payload.day.pnl_by_product?.[product]??0)),
    name:runLabel(payload.run.id),
    type:"bar",
    marker:{color:payload.color},
  })),{barmode:"group",yaxis:{title:"Final PnL"}},{includeZeroY:true});
}
function makeCompareRiskAnalytics(parent,payloads){
  const ddEl=makeChartWrap(parent,"Comparison Drawdown Overlay",300);
  plot(ddEl,payloads.map(payload=>{
    const series=aggregatePnlSeries(payload.data,payload.products);
    return {
      x:series.x,
      y:drawdownSeries(series.y),
      name:runLabel(payload.run.id),
      type:"scatter",
      mode:"lines",
      line:{color:payload.color,width:1.8},
    };
  }),{yaxis:{title:"PnL from peak"}},{includeZeroY:true});

  const scatter=makeChartWrap(parent,"Run Risk / Return Map",280);
  plot(scatter,[{
    x:payloads.map(payload=>payload.drawdown),
    y:payloads.map(payload=>payload.finalPnl),
    text:payloads.map(payload=>`${runLabel(payload.run.id)}<br>PnL ${signed(payload.finalPnl)}<br>DD ${fmt(payload.drawdown)}<br>Trades ${fmt(payload.botTrades)}`),
    type:"scatter",
    mode:"markers+text",
    textposition:"top center",
    marker:{size:payloads.map(payload=>Math.min(30,10+Math.sqrt(payload.botTrades||0))),color:payloads.map(payload=>payload.color),line:{width:1,color:cssVar("--line-strong")}},
    name:"Runs",
  }],{xaxis:{title:"Max Drawdown"},yaxis:{title:"Final PnL"}},{includeZeroY:true});
}
function compareFillRows(payload){
  const fills=payload.products.flatMap(product=>getTradeAnalytics(payload.data,product));
  return {
    fills,
    avgEdge:avg(fills.map(row=>row.edge)),
    avgMarkout10:avg(fills.map(row=>row.markouts[10])),
    avgSpreadCapture:avg(fills.map(row=>row.spreadCapture)),
    takeRate:fills.length?fills.filter(row=>row.crossed).length/fills.length:NaN,
  };
}
function makeCompareExecutionAnalytics(parent,payloads){
  const rows=payloads.map(payload=>Object.assign({payload},compareFillRows(payload)));
  if(!rows.some(row=>row.fills.length))return;
  const el=makeChartWrap(parent,"Comparison Execution Quality",280);
  plot(el,[
    {x:rows.map(row=>runLabel(row.payload.run.id)),y:rows.map(row=>row.avgEdge),name:"Avg edge",type:"bar",marker:{color:rows.map(row=>row.avgEdge>=0?cssVar("--good"):cssVar("--bad"))}},
    {x:rows.map(row=>runLabel(row.payload.run.id)),y:rows.map(row=>row.avgMarkout10),name:"10t markout",type:"bar",marker:{color:cssVar("--accent-2")}},
  ],{barmode:"group",yaxis:{title:"Signed edge"}},{includeZeroY:true});
}
function makeCompareDeltaWaterfall(parent,payloads){
  if(payloads.length<2)return;
  const baseline=payloads[0];
  const challenger=[...payloads].sort((a,b)=>b.finalPnl-a.finalPnl)[0];
  if(!baseline||!challenger||baseline.run.id===challenger.run.id)return;
  const products=[...new Set([...Object.keys(baseline.day.pnl_by_product||{}),...Object.keys(challenger.day.pnl_by_product||{})])];
  const rows=products.map(product=>[
    product,
    Number(challenger.day.pnl_by_product?.[product]??0)-Number(baseline.day.pnl_by_product?.[product]??0),
  ]).sort((a,b)=>Math.abs(b[1])-Math.abs(a[1])).slice(0,28);
  if(!rows.length)return;
  const el=makeChartWrap(parent,`Product Delta Waterfall: ${runLabel(challenger.run.id)} vs ${runLabel(baseline.run.id)}`,340);
  plot(el,[{
    x:rows.map(row=>row[0]),
    y:rows.map(row=>row[1]),
    type:"waterfall",
    measure:rows.map(()=>"relative"),
    connector:{line:{color:cssVar("--line-strong")}},
    increasing:{marker:{color:cssVar("--good")}},
    decreasing:{marker:{color:cssVar("--bad")}},
    totals:{marker:{color:cssVar("--accent-2")}},
    name:"Product delta",
  }],{yaxis:{title:"PnL delta"}},{includeZeroY:true});
}
function makeCompareTable(parent,payloads){
  const rows=[...payloads].sort((a,b)=>b.finalPnl-a.finalPnl);
  const scroller=makeTableWrap(parent,"Comparison Diagnostics");
  scroller.innerHTML=`<table class="data-table">
    <thead><tr><th>Run</th><th>Day</th><th>Trader</th><th>Data</th><th>Final PnL</th><th>Drawdown</th><th>Bot Trades</th><th>Net Qty</th><th>Avg Edge</th><th>Cross Rate</th><th>Products</th></tr></thead>
    <tbody>${rows.map(payload=>{
      const execution=compareFillRows(payload);
      return `<tr>
      <td title="${esc(payload.run.id)}">${esc(runLabel(payload.run.id))}</td>
      <td>${esc(String(payload.day.day??""))}</td>
      <td title="${esc(traderLabel(payload.run,payload.day))}">${esc(traderLabel(payload.run,payload.day))}</td>
      <td title="${esc(dataLabel(payload.day))}">${esc(dataLabel(payload.day))}</td>
      <td class="${pnlClass(payload.finalPnl)}">${signed(payload.finalPnl)}</td>
      <td>${fmt(payload.drawdown)}</td>
      <td>${showBotData?fmt(payload.botTrades):"hidden"}</td>
      <td>${showBotData?fmt(payload.netQty):"hidden"}</td>
      <td class="${pnlClass(execution.avgEdge)}">${showBotData&&Number.isFinite(execution.avgEdge)?signed(execution.avgEdge,2):"-"}</td>
      <td>${showBotData?pct(execution.takeRate):"hidden"}</td>
      <td>${fmt(payload.products.length)}</td>
    </tr>`;
    }).join("")}</tbody>
  </table>`;
}

document.getElementById("refresh-btn").addEventListener("click",()=>refreshRuns());
document.getElementById("clear-runs-btn").addEventListener("click",clearRuns);
document.getElementById("clear-search-btn").addEventListener("click",clearSearch);
document.getElementById("theme-btn").addEventListener("click",toggleTheme);
document.getElementById("search").addEventListener("input",filterRuns);
document.getElementById("day-sel").addEventListener("change",()=>loadDay(true));
document.getElementById("prod-sel").addEventListener("change",redrawActiveView);
document.getElementById("show-bot-btn").addEventListener("click",()=>{
  showBotData=!showBotData;
  setPressed("show-bot-btn",showBotData);
  redrawActiveView();
});
document.getElementById("show-market-btn").addEventListener("click",()=>{
  showMarketTrades=!showMarketTrades;
  setPressed("show-market-btn",showMarketTrades);
  redrawActiveView();
});
document.getElementById("show-spread-btn").addEventListener("click",()=>{
  showSpread=!showSpread;
  setPressed("show-spread-btn",showSpread);
  redrawActiveView();
});
document.getElementById("show-risk-btn").addEventListener("click",()=>{
  showRiskAnalytics=!showRiskAnalytics;
  setPressed("show-risk-btn",showRiskAnalytics);
  redrawActiveView();
});
document.getElementById("show-execution-btn").addEventListener("click",()=>{
  showExecutionAnalytics=!showExecutionAnalytics;
  setPressed("show-execution-btn",showExecutionAnalytics);
  redrawActiveView();
});
document.getElementById("show-book-btn").addEventListener("click",()=>{
  showBookAnalytics=!showBookAnalytics;
  setPressed("show-book-btn",showBookAnalytics);
  redrawActiveView();
});
document.getElementById("show-relation-btn").addEventListener("click",()=>{
  showRelationAnalytics=!showRelationAnalytics;
  setPressed("show-relation-btn",showRelationAnalytics);
  redrawActiveView();
});
document.getElementById("autoscale-btn").addEventListener("click",redrawActiveView);
document.getElementById("cmp-btn").addEventListener("click",openCompare);
document.getElementById("cmp-clr").addEventListener("click",clearCompare);
document.getElementById("run-list").addEventListener("click",event=>{
  const compareButton=event.target.closest("[data-compare-id]");
  if(compareButton){
    event.stopPropagation();
    toggleCompare(compareButton.dataset.compareId);
    return;
  }
  const item=event.target.closest("[data-run-id]");
  if(item)selectRun(item.dataset.runId);
});
document.getElementById("run-list").addEventListener("contextmenu",event=>{
  const item=event.target.closest("[data-run-id]");
  if(!item)return;
  event.preventDefault();
  toggleCompare(item.dataset.runId);
});

init();
</script>
</body>
</html>
"""


def make_handler(store: RunStore, config: ServerConfig) -> type[BaseHTTPRequestHandler]:
    class VisualizerHandler(BaseHTTPRequestHandler):
        server_version = "ProsperityVisualizer/1.0"

        def log_message(self, *_: Any) -> None:
            return

        def _send_json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, body_text: str) -> None:
            body = body_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)

            if parsed.path in ("/", "/index.html"):
                self._send_html(HTML)
                return

            if parsed.path == "/api/config":
                self._send_json(
                    {
                        "allow_delete": config.allow_delete,
                        "run_roots": [str(path) for path in config.run_roots],
                    }
                )
                return

            if parsed.path == "/api/runs":
                self._send_json(store.runs_list)
                return

            if parsed.path == "/api/refresh":
                runs = store.build_index()
                self._send_json({"count": len(runs), "days": store.day_count})
                return

            if parsed.path == "/api/day":
                dir_path = qs.get("dir", [""])[0]
                data = store.load_day(dir_path)
                if data is None:
                    self._send_json({"error": "day log not found"}, status=404)
                    return
                self._send_json(data)
                return

            if parsed.path == "/api/compare":
                ids_raw = qs.get("ids", [""])[0]
                run_ids = [run_id for run_id in ids_raw.split(",") if run_id]
                self._send_json(store.compare(run_ids))
                return

            self._send_json({"error": "not found"}, status=404)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/clear-runs":
                if not config.allow_delete:
                    self._send_json({"error": "delete disabled"}, status=403)
                    return
                self._send_json(store.clear_runs())
                return

            self._send_json({"error": "not found"}, status=404)

    return VisualizerHandler


def parse_args(argv: list[str]) -> ServerConfig:
    parser = argparse.ArgumentParser(
        description="Serve a local dashboard for IMC Prosperity backtest runs."
    )
    parser.add_argument(
        "legacy_port",
        nargs="?",
        help="Optional port, kept for compatibility with older `python visualize.py 8766` usage.",
    )
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help=f"Port to serve on. Default: {DEFAULT_PORT}.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind. Default: 127.0.0.1.")
    parser.add_argument(
        "--runs-dir",
        action="append",
        type=Path,
        default=[],
        help="Additional directory containing run folders. Can be provided multiple times.",
    )
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically.")
    parser.add_argument(
        "--allow-delete",
        action="store_true",
        help="Enable the dashboard button that deletes indexed run folders.",
    )
    args = parser.parse_args(argv)

    port = args.port
    if args.legacy_port is not None:
        try:
            port = int(args.legacy_port)
        except ValueError:
            parser.error("legacy_port must be an integer")

    return ServerConfig(
        host=args.host,
        port=port,
        run_roots=default_run_roots(args.runs_dir),
        open_browser=not args.no_browser,
        allow_delete=args.allow_delete,
    )


def open_browser_later(url: str) -> None:
    time.sleep(0.4)
    webbrowser.open(url)


def print_startup(config: ServerConfig, store: RunStore) -> None:
    print()
    print("  Prosperity Backtest Visualizer")
    print("  " + "-" * 38)
    print(f"  Indexed {len(store.runs_list)} runs ({store.day_count} day files)")
    print("  Run roots:")
    for root in config.run_roots:
        marker = "exists" if root.exists() else "missing"
        print(f"    - {root} [{marker}]")
    print()


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv or sys.argv[1:])
    store = RunStore(config.run_roots)
    url = f"http://{config.host}:{config.port}"

    print_startup(config, store)
    if config.open_browser:
        threading.Thread(target=open_browser_later, args=(url,), daemon=True).start()

    print(f"  Serving at {url}  (Ctrl+C to stop)")
    if not config.allow_delete:
        print("  Delete Runs is disabled. Start with --allow-delete to enable it.")
    print()

    handler = make_handler(store, config)
    try:
        with ThreadingHTTPServer((config.host, config.port), handler) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        print()
        print("  Stopped.")
        return 0
    except OSError as exc:
        print(f"  Could not start server: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
