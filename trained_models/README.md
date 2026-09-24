# Trained Models Directory

This directory is intended to store serialized machine learning models (like `.pkl` files) trained for the agentic trading system.

**Note:** Large model files should typically be excluded from version control. Ensure you add `*.pkl` to your `.gitignore` if you haven't already.

## Usage for Backtesting
You can place your `.pkl` model in this directory and use `backtest_model.py` to evaluate it against historical data:
```bash
python backtest_model.py --model trained_models/my_model.pkl --ticker RELIANCE.NS
```
