# Deploying the Intraday AI engine on the shared server

The server already runs the **Gateway** (`gw-central`, :8000), **snowball** (:8010),
**swing swager** (:8020) and **OptionSmith**, all on ONE real-money Shoonya account.
This app coexists safely: it **reads OptionSmith's `chains.db`** for option chains
(never runs its own OI sweep) and polls `/api/candles` for bars. It runs its own
port (**:8040**), own venv, own DB, own service identity. **Deploy PAPER-first.**

## 1. Get the code
```bash
sudo git clone https://github.com/Sumit-Pluto/Agentic_AI_Trading.git /opt/ai_intraday
# (updates later:  cd /opt/ai_intraday && git pull)
cd /opt/ai_intraday
```

## 2. Python env
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r intraday/requirements.txt
deactivate
```
Needs Python 3.11+ (uses `X | None`). `lightgbm`/`scikit-learn` are for the model
filter; the model unpickles a `quant.pipeline` MetaModel (the `quant/` package
ships in this repo).

## 3. Build the cockpit
```bash
cd frontend && npm install && npm run build && cd ..   # -> frontend/dist
```

## 4. Register a Gateway service identity
```bash
cd <gateway_backend>            # the Gateway's backend dir on this box
python -m scripts.register_service --name ai_intraday \
    --scopes market,orders,positions,funds,status
# prints GATEWAY_CLIENT_ID / GATEWAY_CLIENT_SECRET once — copy them
```

## 5. Configure
```bash
cp intraday/.env.example intraday/.env
# edit intraday/.env:
#   GATEWAY_CLIENT_ID / GATEWAY_CLIENT_SECRET  (from step 4)
#   JWT_SECRET       = the Gateway's JWT_SECRET (must match)
#   OPTIONSMITH_CHAIN_DB = absolute path to OptionSmith's data/chains.db
```
Create `state/intraday_config.json` — **paper-first on live data**, model filter on:
```json
{
  "engine_mode": "live",
  "mode": "paper",
  "model_filter_enabled": true,
  "model_filter_min_prob": 0.50,
  "total_budget": 100000,
  "universe": ["RELIANCE","HDFCBANK","ICICIBANK","INFY","SBIN","TCS","AXISBANK","LT"],
  "bar_timeframe": "5m",
  "demo_step_seconds": 5
}
```
Use underlyings that OptionSmith actually publishes to `chains.db` (its universe is
~212 stocks; indices are off there by default). NIFTY is still used as the regime
index off `/api/candles`.

## 6. Keep the chain store fresh (coexistence)
The engine free-rides on OptionSmith's OI board, which is the single publisher of
`chains.db`. Make sure it's running:
```bash
curl -X POST http://localhost:<optionsmith_port>/api/oiboard/start
```

## 7. Run under systemd
```bash
sudo cp deploy/ai-intraday.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-intraday
sudo journalctl -u ai-intraday -f          # watch it boot
```
Open **http://<server>:8040** → the cockpit. It runs **PAPER on live data**.

## 8. Go LIVE (real money) — only when satisfied
In the cockpit header: **Go LIVE** → type `LIVE` to confirm. This swaps to the
real broker (MIS orders, tagged `source_service=ai_intraday`, so the Gateway never
nets them against snowball/swing NRML). Set your `Total budget` + caps in Settings
first. **Kill** flattens everything and halts. Start/Stop halts new entries while
open positions keep running under the exit engine.

## Coexistence guarantees
- Never drives its own OI sweep → cannot wedge the shared feed.
- MIS product + own `source_service` → no netting against snowball/swing.
- Pre-trade margin gate vs `/api/funds` free margin → won't trip a broker square-off.
- `total_budget` self-cap + soft/hard caps → bounds its footprint on the account.
