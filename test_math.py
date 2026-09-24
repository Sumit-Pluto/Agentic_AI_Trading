import numpy as np
from datetime import datetime, timedelta

def test_math():
    initial_budget = 100000.0
    max_parallel = 4
    
    # Mock some chronological trades
    # PnL % values as returned by labeler (percentage units like 3.62)
    mock_trades = [
        {"time": "09:15", "pnl": 5.0},  # Win
        {"time": "09:30", "pnl": 2.0},  # Win
        {"time": "09:45", "pnl": -3.0}, # Loss
        {"time": "10:00", "pnl": 4.0},  # Win
        {"time": "10:25", "pnl": -10.0}, # Loss (trade 1 is closed)
        {"time": "11:00", "pnl": 1.0},
        {"time": "11:15", "pnl": -5.0},
    ]
    
    current_budget = initial_budget
    active_trades = []
    executed_trades = []
    equity_curve = [current_budget]
    
    print(f"Start Budget: {current_budget}")
    
    for c in mock_trades:
        entry_time = datetime.strptime(c["time"], "%H:%M")
        
        still_active = []
        for t in active_trades:
            if t["exit_time"] <= entry_time:
                current_budget += t["return_amount"]
                print(f"[{c['time']}] Freed capital. Current cash: {current_budget}")
            else:
                still_active.append(t)
        active_trades = still_active
        
        locked_capital = sum(t["capital_used"] for t in active_trades)
        total_equity = current_budget + locked_capital
        
        allocation = total_equity / max_parallel
        print(f"[{c['time']}] Total Equity: {total_equity:.2f} | Allocation: {allocation:.2f}")
        
        if len(active_trades) < max_parallel and current_budget >= (allocation * 0.95):
            trade_capital = min(allocation, current_budget)
            current_budget -= trade_capital
            
            pnl_pct = c["pnl"] / 100.0
            pnl_abs = trade_capital * pnl_pct
            
            exit_time = entry_time + timedelta(minutes=60)
            
            print(f"[{c['time']}] Executing with cap: {trade_capital:.2f}, PnL%: {pnl_pct:.2%}, Abs: {pnl_abs:.2f}")
            
            active_trades.append({
                "exit_time": exit_time,
                "capital_used": trade_capital,
                "return_amount": trade_capital + pnl_abs
            })
            
            equity_curve.append(total_equity)
            executed_trades.append(c)
        else:
            print(f"[{c['time']}] Skipped due to capacity/budget.")
            
    for t in active_trades:
        current_budget += t["return_amount"]
    equity_curve.append(current_budget)
    print(f"Final Realized Budget: {current_budget}")
    
    print("\nEquity Curve:", [round(x, 2) for x in equity_curve])
    
    eq_arr = np.array(equity_curve)
    peak = np.maximum.accumulate(eq_arr)
    drawdowns = (eq_arr - peak) / peak
    max_drawdown = float(drawdowns.min()) if len(drawdowns) else 0
    print(f"Max Drawdown: {max_drawdown:.2%}")

if __name__ == "__main__":
    test_math()
