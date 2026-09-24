import argparse
import pickle
import numpy as np
import pandas as pd
import logging
import os
import random
from datetime import datetime, timedelta

from quant.pipeline import realdata
from quant.pipeline import pipeline as P
from quant.pipeline import model as M

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_available_tickers(data_dir):
    minute_dir = os.path.join(data_dir, "raw", "minute_candles")
    if not os.path.exists(minute_dir):
        return []
    tickers = set()
    for f in os.listdir(minute_dir):
        if f.endswith("_5min.csv"):
            ticker = f.replace("_5min.csv", "")
            if ticker not in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]:
                tickers.add(ticker)
    return sorted(list(tickers))

def calc_metrics(trades):
    if not trades:
        return {"win_rate": 0, "avg_pnl": 0, "profit_factor": 0, "trades": 0, "expectancy": 0}
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    
    win_rate = len(wins) / len(trades)
    avg_pnl = sum(pnls) / len(pnls)
    avg_win = sum(wins)/len(wins) if wins else 0
    avg_loss = sum(losses)/len(losses) if losses else 0
    pf = abs(sum(wins)/sum(losses)) if sum(losses) != 0 else float('inf')
    exp = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)
    
    return {
        "trades": len(trades),
        "win_rate": win_rate,
        "avg_pnl": avg_pnl,
        "profit_factor": pf,
        "expectancy": exp
    }

def run_backtest(model_path, data_dir, start, end, num_stocks=10, tickers=None, threshold=0.55, initial_budget=100000, max_parallel=4, progress_cb=logging.info):
    if tickers:
        selected_tickers = tickers
    else:
        available = get_available_tickers(data_dir)
        if not available:
            raise ValueError(f"No tickers found in {data_dir}/raw/minute_candles")
        random.seed(42)
        selected_tickers = random.sample(available, min(num_stocks, len(available)))
        
    progress_cb(f"Selected {len(selected_tickers)} tickers for backtesting: {', '.join(selected_tickers)}")
    progress_cb(f"Loading data from {data_dir} from {start} to {end}")
    
    dd = realdata.load(symbols=selected_tickers, start=start, end=end, data_dir=data_dir)
    
    if len(dd.days) == 0:
        raise ValueError("No data loaded. Check dates/directory.")

    progress_cb(f"Loaded {len(dd.days)} days of data. Building feature matrix...")
    cands, bank = P.build_training_set(dd, progress=progress_cb)
    
    if not cands:
        raise ValueError("No trading candidates found.")

    X = M.build_matrix(cands)
    progress_cb(f"Loading model from {model_path}")
    with open(model_path, "rb") as f:
        loaded = pickle.load(f)
    model = loaded["model"] if isinstance(loaded, dict) and "model" in loaded else loaded
        
    progress_cb("Generating predictions...")
    preds = model.predict(X)
    
    # Attach predictions and sort candidates chronologically
    for c, p in zip(cands, preds):
        c.p_win = float(p)
    
    cands.sort(key=lambda x: x.event.bar_time)
    
    # Event-driven budget simulation
    progress_cb(f"Simulating trades with budget ₹{initial_budget:,.2f} and max {max_parallel} parallel trades...")
    
    current_budget = float(initial_budget)
    active_trades = []
    executed_trades = []
    equity_curve = [current_budget]
    
    for c in cands:
        if c.p_win < threshold:
            continue
            
        entry_time = pd.Timestamp(c.event.bar_time)
        
        # Free up budget from closed trades (assume exit after 60 mins / 12 bars)
        still_active = []
        for t in active_trades:
            if t["exit_time"] <= entry_time:
                current_budget += t["return_amount"]
            else:
                still_active.append(t)
        active_trades = still_active
        
        # Calculate total current equity (cash + capital locked in active trades)
        locked_capital = sum(t["capital_used"] for t in active_trades)
        total_equity = current_budget + locked_capital
        
        if total_equity <= 0:
            continue
            
        # Check if we have budget for a new trade
        allocation = total_equity / max_parallel
        
        if len(active_trades) < max_parallel and current_budget >= (allocation * 0.95):
            # Execute trade
            trade_capital = min(allocation, current_budget)
            current_budget -= trade_capital
            
            pnl_pct = c.outcome_pnl_pct / 100.0
            pnl_abs = trade_capital * pnl_pct
            
            exit_time = entry_time + timedelta(minutes=60)
            
            trade_info = {
                "symbol": c.event.symbol,
                "direction": c.event.direction,
                "entry_time": entry_time.strftime("%Y-%m-%d %H:%M"),
                "exit_time": exit_time.strftime("%Y-%m-%d %H:%M"),
                "entry_price": float(c.event.entry_price),
                "pnl_pct": pnl_pct,
                "pnl_abs": pnl_abs,
                "capital_used": trade_capital,
                "reason": c.exit_reason
            }
            executed_trades.append(trade_info)
            
            active_trades.append({
                "exit_time": exit_time,
                "capital_used": trade_capital,
                "return_amount": trade_capital + pnl_abs
            })
            
    # Close any remaining active trades at the end
    for t in active_trades:
        current_budget += t["return_amount"]
        
    if not executed_trades:
        raise ValueError("No trades executed under these constraints.")
        
    # Rebuild a perfectly chronological realized equity curve
    executed_trades.sort(key=lambda x: x["exit_time"])
    realized_budget = float(initial_budget)
    equity_curve = [realized_budget]
    for t in executed_trades:
        realized_budget += t["pnl_abs"]
        equity_curve.append(realized_budget)
        
    total_return = (realized_budget - initial_budget) / initial_budget
    
    eq_arr = np.array(equity_curve)
    peak = np.maximum.accumulate(eq_arr)
    drawdowns = (eq_arr - peak) / peak
    max_drawdown = float(drawdowns.min()) if len(drawdowns) else 0
    overall_metrics = calc_metrics(executed_trades)
    buy_trades = [t for t in executed_trades if t["direction"] == "BUY"]
    sell_trades = [t for t in executed_trades if t["direction"] == "SELL"]
    buy_metrics = calc_metrics(buy_trades)
    sell_metrics = calc_metrics(sell_trades)
    
    progress_cb(f"Simulation Complete. Final Budget: ₹{realized_budget:,.2f} ({total_return*100:.2f}%).")
    
    return {
        "tickers": selected_tickers,
        "initial_budget": initial_budget,
        "final_budget": realized_budget,
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "overall": overall_metrics,
        "buy": buy_metrics,
        "sell": sell_metrics,
        "equity_curve": equity_curve,
        "trades": executed_trades
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=str, default='state/model_full.pkl')
    parser.add_argument('--num_stocks', type=int, default=10)
    parser.add_argument('--tickers', type=str, nargs='+')
    parser.add_argument('--start', type=str, default='2025-05-01')
    parser.add_argument('--end', type=str, default='2025-11-01')
    parser.add_argument('--data_dir', type=str, default='/Users/mac/Downloads/Options_data')
    parser.add_argument('--threshold', type=float, default=0.55)
    parser.add_argument('--budget', type=float, default=100000)
    parser.add_argument('--parallel', type=int, default=4)
    args = parser.parse_args()
    
    res = run_backtest(args.model, args.data_dir, args.start, args.end, args.num_stocks, args.tickers, args.threshold, args.budget, args.parallel)
    logging.info(f"Final Return: {res['total_return']*100:.2f}%")

if __name__ == "__main__":
    main()
