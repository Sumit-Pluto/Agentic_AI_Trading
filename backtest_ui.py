#!/usr/bin/env python3
"""Standalone backtest UI — open in a browser and click Run. No broker/login.

    conda activate sm_agent
    python backtest_ui.py           # then open http://127.0.0.1:8500

Runs the trained-vs-default agent-weight backtest on the locally cached HF data
(data/hf_cache). Shows the last saved full-year result immediately; the form
runs a fresh backtest in the background with a live progress log.
"""
import json
import os
import threading

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from quant.training.backtester import compare
from quant.training.hf_data import cached_symbols

app = FastAPI()
STATE = {"running": False, "log": [], "result": None, "error": None}
RESULT_FILE = "backtest_result.json"
VIX = "/Users/mac/Downloads/div_4_doc/Trading/Trading_project_2.0/state/vix_daily.json"


def _slim(cmp):
    out = {"symbols": cmp["symbols"], "start": cmp["start"], "end": cmp["end"],
           "default": {}, "trained": {}}
    for side in ("default", "trained"):
        for mode, blk in cmp[side]["by_mode"].items():
            out[side][mode] = {"overall": blk["overall"],
                               "by_symbol": blk["by_symbol"],
                               "trades": blk.get("trades", [])}
    return out


def _job(weights, symbols, start, end, modes, stride):
    STATE.update(running=True, log=[], result=None, error=None)
    try:
        cmp = compare(weights, symbols, start, end,
                      vix_path=VIX if os.path.exists(VIX) else None,
                      modes=modes, stride=stride,
                      progress=lambda m: STATE["log"].append(m))
        STATE["result"] = _slim(cmp)
        with open(RESULT_FILE, "w") as f:
            json.dump(STATE["result"], f, indent=2)
    except Exception as e:
        STATE["error"] = str(e)
        STATE["log"].append("ERROR: " + str(e))
    finally:
        STATE["running"] = False


@app.get("/api/symbols")
def api_symbols():
    return {"symbols": cached_symbols("data/hf_cache")}


@app.get("/api/last")
def api_last():
    if os.path.exists(RESULT_FILE):
        try:
            return json.load(open(RESULT_FILE))
        except Exception:
            pass
    return {}


@app.get("/api/status")
def api_status():
    return STATE


@app.post("/api/run")
async def api_run(req: Request):
    if STATE["running"]:
        return {"ok": False, "msg": "a backtest is already running"}
    b = await req.json()
    modes = tuple(b.get("modes") or ["confirm", "agents"])
    threading.Thread(target=_job, kwargs=dict(
        weights=b.get("weights") or "weights_3stocks.json",
        symbols=b.get("symbols") or None,
        start=b.get("start") or "2025-01-01",
        end=b.get("end") or "2026-01-22",
        modes=modes, stride=int(b.get("stride") or 5)), daemon=True).start()
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Backtest</title>
<style>
 :root{--bg:#0f1319;--panel:#161c25;--ink:#e7ebf1;--mut:#93a0b1;--line:#26303c;
   --acc:#5b8bd0;--gain:#37c08a;--loss:#e8756f;--mono:ui-monospace,Menlo,Consolas,monospace}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
   font:15px/1.5 system-ui,Arial,sans-serif}
 .wrap{max-width:960px;margin:0 auto;padding:28px 20px 60px}
 h1{font-size:24px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 22px;font-size:14px}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin:16px 0}
 label{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:6px}
 .row{display:flex;flex-wrap:wrap;gap:18px;align-items:flex-end}
 .row>div{flex:0 0 auto}
 input,select{background:#0d1219;color:var(--ink);border:1px solid var(--line);
   border-radius:8px;padding:8px 10px;font:14px var(--mono)}
 .chk{display:inline-flex;gap:6px;align-items:center;margin-right:14px;color:var(--ink);font-size:14px;text-transform:none;letter-spacing:0}
 button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:10px 20px;
   font-size:15px;font-weight:600;cursor:pointer}
 button:disabled{opacity:.5;cursor:default}
 .scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px;margin-top:6px}
 caption{text-align:left;font-weight:700;padding:6px 0;color:var(--acc)}
 th,td{padding:8px 12px;text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
 th:first-child,td:first-child{text-align:left;font-family:system-ui}
 thead th{color:var(--mut);border-bottom:1px solid var(--line);font-weight:600;text-transform:uppercase;font-size:11px}
 tbody tr+tr td{border-top:1px solid var(--line)}
 .pos{color:var(--gain)}.neg{color:var(--loss)}.mut{color:var(--mut)}
 #log{font-family:var(--mono);font-size:12px;color:var(--mut);white-space:pre-wrap;
   max-height:160px;overflow:auto;margin-top:10px}
 #tvtable{max-height:440px;overflow:auto}
 #tvtable thead th{position:sticky;top:0;background:var(--panel)}
 .spin{display:inline-block;width:14px;height:14px;border:2px solid var(--line);
   border-top-color:var(--acc);border-radius:50%;animation:s .8s linear infinite;vertical-align:-2px;margin-right:8px}
 @keyframes s{to{transform:rotate(360deg)}}
 .warn{border-left:3px solid var(--loss);padding-left:12px;color:var(--mut);font-size:13px;margin-top:10px}
</style></head><body><div class="wrap">
 <h1>Agent-Weight Backtest</h1>
 <p class="sub">Trained per-stock weights vs. untrained defaults — on locally cached NSE minute data. No login needed.</p>

 <div class="card">
   <div class="row">
     <div><label>Stocks</label><span id="stocks"></span></div>
     <div><label>Start</label><input id="start" value="2025-12-01" size="10"></div>
     <div><label>End</label><input id="end" value="2026-01-22" size="10"></div>
     <div><label>Stride</label><input id="stride" value="8" size="3"></div>
     <div><label>Modes</label>
       <span class="chk"><input type="checkbox" id="m_confirm" checked>indicator+agent</span>
       <span class="chk"><input type="checkbox" id="m_agents" checked>naked agents</span>
     </div>
     <div><button id="run" onclick="run()">Run Backtest</button></div>
   </div>
   <div id="log"></div>
   <div class="warn">Full year × 3 stocks takes several minutes. Shorten the dates or raise the stride for a faster demo. Last saved result loads automatically below.</div>
 </div>

 <div id="results"></div>

<script>
const F=(v)=>v==null?'–':(typeof v==='number'?v.toFixed(v%1?3:0):v);
const cls=(v)=>typeof v==='number'?(v>0?'pos':(v<0?'neg':'')):'';
async function loadStocks(){
  const s=await (await fetch('/api/symbols')).json();
  document.getElementById('stocks').innerHTML=(s.symbols||[]).map(x=>
    `<span class="chk"><input type="checkbox" class="sy" value="${x}" checked>${x}</span>`).join('');
}
function chosen(){return [...document.querySelectorAll('.sy:checked')].map(e=>e.value);}
function modes(){let m=[];if(m_confirm.checked)m.push('confirm');if(m_agents.checked)m.push('agents');return m;}
const MET=[['trades','Trades'],['win_rate','Win %'],['total_pnl_pct','Total P&L %'],
  ['expectancy_pct','Expectancy %'],['profit_factor','Profit factor'],['max_drawdown_pct','Max DD %']];
function table(mode,def,tr){
  let rows=MET.map(([k,lab])=>{
    let d=def.overall[k],t=tr.overall[k],delta=(typeof d==='number'&&typeof t==='number')?(t-d):null;
    return `<tr><td>${lab}</td><td class="${cls(d)}">${F(d)}</td><td class="${cls(t)}">${F(t)}</td>
      <td class="${cls(delta)}">${delta==null?'':(delta>0?'+':'')+F(delta)}</td></tr>`}).join('');
  let syms=Object.keys(tr.by_symbol||{});
  let per=syms.map(s=>{let d=def.by_symbol[s],t=tr.by_symbol[s];
    let changed=d.trades!==t.trades||d.total_pnl_pct!==t.total_pnl_pct;
    return `<tr><td>${s} ${changed?'':'<span class=mut>(unchanged)</span>'}</td>
      <td class=mut>${d.trades}→${t.trades}</td>
      <td>${F(d.win_rate)}→${F(t.win_rate)}</td>
      <td class="${cls(t.total_pnl_pct)}">${F(d.total_pnl_pct)}→${F(t.total_pnl_pct)}</td>
      <td>${F(d.profit_factor)}→${F(t.profit_factor)}</td></tr>`}).join('');
  return `<div class="card"><div class="scroll"><table>
    <caption>${mode==='confirm'?'Indicator + Agent':'Naked Agent buy/sell'} — overall</caption>
    <thead><tr><th>Metric</th><th>Default</th><th>Trained</th><th>Δ</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <table style="margin-top:14px"><caption>per stock (default→trained)</caption>
    <thead><tr><th>Stock</th><th>Trades</th><th>Win %</th><th>P&L %</th><th>Profit factor</th></tr></thead>
    <tbody>${per}</tbody></table></div></div>`;
}
let CUR=null;
function render(r){
  CUR=r;
  if(!r||!r.trained){document.getElementById('results').innerHTML='';return;}
  let modes=Object.keys(r.trained);
  let html=`<p class="sub">Window ${r.start} → ${r.end} · ${(r.symbols||[]).join(', ')}</p>`;
  for(const mode of modes) html+=table(mode,r.default[mode],r.trained[mode]);
  // trades browser
  const hasTrades=modes.some(m=>(r.trained[m].trades||[]).length||(r.default[m].trades||[]).length);
  html+=`<div class="card"><label>Individual trades</label>`;
  if(!hasTrades){
    html+=`<p class="mut" style="margin:6px 0 0">This saved result predates trade-logging. Click <b>Run Backtest</b> (any window) to see the full trade list.</p></div>`;
  }else{
    html+=`<div class="row" style="margin-bottom:10px">
      <div><label>Weights</label><select id="tvside"><option value="trained">trained</option><option value="default">default</option></select></div>
      <div><label>Mode</label><select id="tvmode">${modes.map(m=>`<option value="${m}">${m==='confirm'?'indicator+agent':'naked agents'}</option>`).join('')}</select></div>
      <div><label>&nbsp;</label><span class="mut" id="tvcount"></span></div>
    </div><div class="scroll" id="tvtable"></div></div>`;
  }
  document.getElementById('results').innerHTML=html;
  if(hasTrades){
    document.getElementById('tvside').onchange=renderTrades;
    document.getElementById('tvmode').onchange=renderTrades;
    renderTrades();
  }
}
function renderTrades(){
  if(!CUR)return;
  const side=document.getElementById('tvside').value, mode=document.getElementById('tvmode').value;
  const tr=(CUR[side][mode].trades||[]).slice().sort((a,b)=>a.entry_time<b.entry_time?-1:1);
  document.getElementById('tvcount').textContent=tr.length+' trades';
  let cum=0;
  const rows=tr.map((t,i)=>{cum+=t.pnl_pct||0;
    return `<tr><td>${i+1}</td><td>${t.symbol}</td><td>${(t.entry_time||'').replace('T',' ').slice(0,16)}</td>
      <td class="${t.direction==='BUY'?'pos':'neg'}">${t.direction}</td>
      <td>${F(t.entry)}</td><td class="mut">${t.exit_reason||''}</td>
      <td>${F(t.score)}</td><td class="${cls(t.pnl_pct)}">${t.pnl_pct>0?'+':''}${F(t.pnl_pct)}</td>
      <td class="${cls(cum)}">${cum>0?'+':''}${cum.toFixed(2)}</td></tr>`}).join('');
  document.getElementById('tvtable').innerHTML=`<table><thead><tr>
    <th>#</th><th>Symbol</th><th>Entry time</th><th>Dir</th><th>Entry</th>
    <th>Exit</th><th>Score</th><th>P&L %</th><th>Cum %</th></tr></thead>
    <tbody>${rows||'<tr><td colspan=9 class=mut>no trades</td></tr>'}</tbody></table>`;
}
async function poll(){
  const s=await (await fetch('/api/status')).json();
  const btn=document.getElementById('run'),log=document.getElementById('log');
  if(s.running){btn.disabled=true;btn.innerHTML='<span class=spin></span>Running…';
    log.textContent=(s.log||[]).slice(-12).join('\n');setTimeout(poll,1500);}
  else{btn.disabled=false;btn.textContent='Run Backtest';
    if(s.result){render(s.result);log.textContent=(s.log||[]).slice(-3).join('\n')||'done.';}
    else if(s.error){log.textContent='ERROR: '+s.error;}}
}
async function run(){
  const body={symbols:chosen(),start:start.value,end:end.value,stride:stride.value,modes:modes()};
  const r=await (await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)})).json();
  if(!r.ok){alert(r.msg||'could not start');return;}
  poll();
}
loadStocks();
fetch('/api/last').then(r=>r.json()).then(render);
</script></div></body></html>"""


if __name__ == "__main__":
    port = int(os.getenv("BACKTEST_UI_PORT", "8500"))
    print(f"\n  Backtest UI  ->  http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
