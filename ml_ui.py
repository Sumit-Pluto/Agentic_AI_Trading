#!/usr/bin/env python3
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import threading
import json
from backtest_model import run_backtest

app = FastAPI(title="ML Backtest UI")

STATE = {
    "running": False,
    "log": [],
    "result": None,
    "error": None
}

def backtest_job(model_path, data_dir, start, end, num_stocks, tickers, threshold, budget, parallel):
    STATE["running"] = True
    STATE["log"] = []
    STATE["result"] = None
    STATE["error"] = None
    
    def log_progress(msg):
        STATE["log"].append(msg)
        
    try:
        res = run_backtest(
            model_path=model_path,
            data_dir=data_dir,
            start=start,
            end=end,
            num_stocks=num_stocks,
            tickers=tickers,
            threshold=threshold,
            initial_budget=budget,
            max_parallel=parallel,
            progress_cb=log_progress
        )
        STATE["result"] = res
    except Exception as e:
        STATE["error"] = str(e)
        log_progress(f"ERROR: {str(e)}")
    finally:
        STATE["running"] = False

@app.post("/api/run")
async def api_run(req: Request):
    if STATE["running"]:
        return {"ok": False, "msg": "Backtest is already running!"}
    
    body = await req.json()
    model_path = body.get("model_path", "state/model_full.pkl")
    data_dir = body.get("data_dir", "/Users/mac/Downloads/Options_data")
    start = body.get("start", "2025-05-01")
    end = body.get("end", "2025-11-01")
    num_stocks = int(body.get("num_stocks", 10))
    threshold = float(body.get("threshold", 0.55))
    budget = float(body.get("budget", 100000))
    parallel = int(body.get("parallel", 4))
    
    raw_tickers = body.get("tickers", "").strip()
    tickers = [t.strip() for t in raw_tickers.split(",")] if raw_tickers else None
    
    threading.Thread(
        target=backtest_job, 
        args=(model_path, data_dir, start, end, num_stocks, tickers, threshold, budget, parallel),
        daemon=True
    ).start()
    
    return {"ok": True}

@app.get("/api/status")
def api_status():
    return STATE

@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ML Backtest UI</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0b0f19;
            --panel: rgba(22, 28, 37, 0.7);
            --border: rgba(255, 255, 255, 0.1);
            --accent: #3b82f6;
            --accent-hover: #2563eb;
            --text-main: #f1f5f9;
            --text-muted: #94a3b8;
            --success: #10b981;
            --danger: #ef4444;
            --glass: rgba(255, 255, 255, 0.03);
        }
        body {
            background-color: var(--bg);
            color: var(--text-main);
            font-family: 'Inter', sans-serif;
            margin: 0; padding: 2rem;
            background-image: radial-gradient(circle at 50% -20%, #1e3a8a 0%, var(--bg) 50%);
            min-height: 100vh;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            display: grid;
            grid-template-columns: 350px 1fr;
            gap: 2rem;
        }
        .glass-panel {
            background: var(--panel);
            backdrop-filter: blur(16px);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 1.5rem;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
        }
        h2 { margin-top: 0; font-size: 1.25rem; font-weight: 600; border-bottom: 1px solid var(--border); padding-bottom: 0.75rem; }
        h3 { font-size: 1rem; color: #cbd5e1; margin-top: 2rem; border-bottom: 1px solid var(--border); padding-bottom: 0.5rem; }
        .form-group { margin-bottom: 1.25rem; }
        label { display: block; font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.5rem; font-weight: 600; }
        input {
            width: 100%; background: var(--glass); border: 1px solid var(--border); color: #fff;
            padding: 0.75rem; border-radius: 8px; font-family: inherit; font-size: 0.9rem; box-sizing: border-box;
        }
        input:focus { outline: none; border-color: var(--accent); }
        button {
            width: 100%; background: linear-gradient(135deg, var(--accent), #6366f1); color: white;
            border: none; padding: 1rem; border-radius: 8px; font-weight: 600; cursor: pointer; margin-top: 1rem;
        }
        button:disabled { background: #334155; cursor: not-allowed; }
        
        .metrics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 1rem; margin-bottom: 1rem;}
        .metric-card { background: var(--glass); border: 1px solid var(--border); padding: 1rem; border-radius: 12px; text-align: center; }
        .metric-card span { display: block; font-size: 0.75rem; color: var(--text-muted); margin-bottom: 0.5rem; text-transform: uppercase; }
        .metric-card .val { font-size: 1.25rem; font-weight: 700; color: #fff; }
        .val.positive { color: var(--success); }
        .val.negative { color: var(--danger); }
        
        .chart-container { background: var(--glass); border: 1px solid var(--border); border-radius: 12px; padding: 1rem; min-height: 350px; margin-top:1.5rem;}
        
        table { width: 100%; border-collapse: collapse; margin-top: 1rem; font-size: 0.85rem;}
        th, td { text-align: left; padding: 0.75rem; border-bottom: 1px solid var(--border); }
        th { color: var(--text-muted); font-weight: 600; text-transform: uppercase; font-size: 0.75rem;}
        .td-buy { color: var(--success); font-weight: 600; }
        .td-sell { color: var(--danger); font-weight: 600; }
        
        .log-box { background: rgba(0,0,0,0.4); border-radius: 8px; padding: 1rem; font-family: monospace; font-size: 0.8rem; height: 120px; overflow-y: auto; margin-top: 1.5rem; border: 1px solid var(--border);}
        .loader { border: 3px solid rgba(255,255,255,0.1); border-top: 3px solid #fff; border-radius: 50%; width: 14px; height: 14px; animation: spin 1s linear infinite; display: inline-block; vertical-align: middle; margin-left: 8px; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
    </style>
</head>
<body>

<div class="container">
    <div class="glass-panel">
        <h2>Configuration</h2>
        <div class="form-group"><label>Model</label><input type="text" id="model_path" value="state/model_full.pkl"></div>
        <div class="form-group"><label>Budget (₹)</label><input type="number" id="budget" value="100000"></div>
        <div class="form-group"><label>Max Parallel Trades</label><input type="number" id="parallel" value="4"></div>
        <div class="form-group"><label>Start Date</label><input type="date" id="start_date" value="2025-05-01"></div>
        <div class="form-group"><label>End Date</label><input type="date" id="end_date" value="2025-11-01"></div>
        <div class="form-group"><label>Num Stocks</label><input type="number" id="num_stocks" value="10"></div>
        <div class="form-group"><label>Specific Tickers</label><input type="text" id="tickers" placeholder="RELIANCE, HDFCBANK"></div>
        <div class="form-group"><label>Probability Threshold</label><input type="number" id="threshold" value="0.55" step="0.01"></div>
        <button id="run_btn" onclick="startBacktest()">Run Backtest</button>
    </div>

    <div class="results-area">
        <div class="glass-panel">
            <h2>Results</h2>
            
            <div id="results_wrapper" style="display: none;">
                <h3>Overall Performance</h3>
                <div class="metrics-grid">
                    <div class="metric-card"><span>Final Budget</span><div class="val" id="val_budget">--</div></div>
                    <div class="metric-card"><span>Total Return</span><div class="val" id="val_ret">--</div></div>
                    <div class="metric-card"><span>Max Drawdown</span><div class="val" id="val_mdd">--</div></div>
                    <div class="metric-card"><span>Win Rate</span><div class="val" id="val_wr">--</div></div>
                    <div class="metric-card"><span>Total Trades</span><div class="val" id="val_tr">--</div></div>
                    <div class="metric-card"><span>Expectancy</span><div class="val" id="val_exp">--</div></div>
                </div>

                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                    <div>
                        <h3 style="color: var(--success);">BUY Trades</h3>
                        <div class="metrics-grid">
                            <div class="metric-card"><span>Trades</span><div class="val" id="b_tr">--</div></div>
                            <div class="metric-card"><span>Win Rate</span><div class="val" id="b_wr">--</div></div>
                            <div class="metric-card"><span>Avg PnL</span><div class="val" id="b_avg">--</div></div>
                        </div>
                    </div>
                    <div>
                        <h3 style="color: var(--danger);">SELL Trades</h3>
                        <div class="metrics-grid">
                            <div class="metric-card"><span>Trades</span><div class="val" id="s_tr">--</div></div>
                            <div class="metric-card"><span>Win Rate</span><div class="val" id="s_wr">--</div></div>
                            <div class="metric-card"><span>Avg PnL</span><div class="val" id="s_avg">--</div></div>
                        </div>
                    </div>
                </div>

                <div class="chart-container"><canvas id="equityChart"></canvas></div>
                
                <h3>Trade Log</h3>
                <div style="overflow-x: auto; max-height: 400px; overflow-y: auto;">
                    <table id="trade_table">
                        <thead>
                            <tr>
                                <th>Symbol</th>
                                <th>Dir</th>
                                <th>Entry Time</th>
                                <th>Exit Time</th>
                                <th>Entry Price</th>
                                <th>Capital</th>
                                <th>PnL %</th>
                                <th>PnL ₹</th>
                                <th>Reason</th>
                            </tr>
                        </thead>
                        <tbody></tbody>
                    </table>
                </div>
            </div>
            
            <div id="log-container" class="log-box"><div class="log-line">Ready.</div></div>
        </div>
    </div>
</div>

<script>
    let poll = null, chart = null;
    
    function fmtPct(val) { return (val * 100).toFixed(2) + '%'; }
    function fmtCurr(val) { return '₹' + val.toLocaleString('en-IN', {maximumFractionDigits: 2}); }
    function colorClass(val) { return val > 0 ? 'positive' : (val < 0 ? 'negative' : ''); }
    function setVal(id, text, cls='') { const el = document.getElementById(id); el.innerText = text; if(cls) el.className = 'val ' + cls; }

    async function startBacktest() {
        document.getElementById('run_btn').disabled = true;
        document.getElementById('run_btn').innerHTML = 'Running <div class="loader"></div>';
        document.getElementById('results_wrapper').style.display = 'none';
        
        await fetch('/api/run', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                model_path: document.getElementById('model_path').value,
                budget: document.getElementById('budget').value,
                parallel: document.getElementById('parallel').value,
                start: document.getElementById('start_date').value,
                end: document.getElementById('end_date').value,
                num_stocks: document.getElementById('num_stocks').value,
                tickers: document.getElementById('tickers').value,
                threshold: document.getElementById('threshold').value
            })
        });
        poll = setInterval(checkStatus, 1000);
    }

    async function checkStatus() {
        const res = await fetch('/api/status');
        const state = await res.json();
        
        const logBox = document.getElementById('log-container');
        logBox.innerHTML = state.log.map(l => `<div style="color: ${l.includes('ERROR')?'var(--danger)':'inherit'}">${l}</div>`).join('');
        logBox.scrollTop = logBox.scrollHeight;

        if (!state.running) {
            clearInterval(poll);
            document.getElementById('run_btn').disabled = false;
            document.getElementById('run_btn').innerHTML = 'Run Backtest';
            if (state.result) renderResults(state.result);
        }
    }
    
    function renderResults(r) {
        document.getElementById('results_wrapper').style.display = 'block';
        
        // Overall
        setVal('val_budget', fmtCurr(r.final_budget));
        setVal('val_ret', fmtPct(r.total_return), colorClass(r.total_return));
        setVal('val_mdd', fmtPct(r.max_drawdown), 'negative');
        setVal('val_wr', fmtPct(r.overall.win_rate), r.overall.win_rate >= 0.5 ? 'positive' : 'negative');
        setVal('val_tr', r.overall.trades);
        setVal('val_exp', fmtPct(r.overall.expectancy), colorClass(r.overall.expectancy));
        
        // Buy
        setVal('b_tr', r.buy.trades);
        setVal('b_wr', fmtPct(r.buy.win_rate), r.buy.win_rate >= 0.5 ? 'positive' : '');
        setVal('b_avg', fmtPct(r.buy.avg_pnl), colorClass(r.buy.avg_pnl));
        
        // Sell
        setVal('s_tr', r.sell.trades);
        setVal('s_wr', fmtPct(r.sell.win_rate), r.sell.win_rate >= 0.5 ? 'positive' : '');
        setVal('s_avg', fmtPct(r.sell.avg_pnl), colorClass(r.sell.avg_pnl));
        
        // Chart
        const ctx = document.getElementById('equityChart').getContext('2d');
        if (chart) chart.destroy();
        chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: r.equity_curve.map((_, i) => i === 0 ? 'Start' : 'T'+i),
                datasets: [{
                    label: 'Budget (₹)', data: r.equity_curve,
                    borderColor: '#3b82f6', backgroundColor: 'rgba(59, 130, 246, 0.1)',
                    borderWidth: 2, pointRadius: 0, fill: true, tension: 0.1
                }]
            },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: {display: false} },
                       scales: { x: { grid: {color: 'rgba(255,255,255,0.05)'} }, y: { grid: {color: 'rgba(255,255,255,0.05)'} } } }
        });
        
        // Table
        const tbody = document.querySelector('#trade_table tbody');
        tbody.innerHTML = r.trades.map(t => `
            <tr>
                <td>${t.symbol}</td>
                <td class="${t.direction === 'BUY' ? 'td-buy' : 'td-sell'}">${t.direction}</td>
                <td>${t.entry_time}</td>
                <td>${t.exit_time}</td>
                <td>${fmtCurr(t.entry_price)}</td>
                <td>${fmtCurr(t.capital_used)}</td>
                <td class="${colorClass(t.pnl_pct)}">${fmtPct(t.pnl_pct)}</td>
                <td class="${colorClass(t.pnl_abs)}">${fmtCurr(t.pnl_abs)}</td>
                <td>${t.reason}</td>
            </tr>
        `).join('');
    }
</script>
</body>
</html>
"""

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8501)
