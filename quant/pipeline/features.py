"""quant/pipeline/features.py — LAYER B feature enrichment.

Turns a TriggerEvent into a feature vector, pulling from every module family:
candle/price, regime, options-chain (computed: PCR, wall-distance-in-ATR, GEX via
Black-Scholes gamma, ATM-IV, 25d skew), sector/relative-strength, calendar/era,
microstructure-lite. Indicators are precomputed once per stock for speed.

In production some of these are computed live (IV/greeks from the broker chain) and
some downloaded (bhavcopy OI) — see docs/DATA_REQUIREMENTS_FOR_REVIEW.md. Here they
are all computed from the synthetic bundle so the pipeline is testable offline.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import norm

# feature schema shared with the model
NUMERIC = [
    "ret_1", "ret_3", "ret_6", "atr_pct", "rsi", "trend", "rv_20", "vwap_dev",
    "rel_vol", "range_pos", "rel_strength", "beta",
    "vix", "pcr_oi", "dist_call_wall_atr", "dist_put_wall_atr", "gex", "atm_iv",
    "skew_25", "total_oi_z", "dte", "days_to_event", "tod_bucket", "dow",
    "spread_ticks", "depth_imb", "dir_sign",
]
CATEGORICAL = ["strategy_id", "setup_id", "era", "liq_tier"]


# ── indicators ───────────────────────────────────────────────────────────────
def _rma(x, n):
    return x.ewm(alpha=1 / n, adjust=False).mean()


def _atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return _rma(tr, n)


def _rsi(c, n=14):
    d = c.diff()
    up = _rma(d.clip(lower=0), n)
    dn = _rma((-d).clip(lower=0), n)
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def _session_vwap(df):
    day = df.index.normalize()
    pv = (df["close"] * df["volume"]).groupby(day).cumsum()
    vv = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return pv / vv


class FeatureBank:
    """Precomputed per-stock indicator columns + index return series."""

    def __init__(self, dd):
        self.dd = dd
        self.cols = {}
        idx_ret = dd.index["close"].pct_change().rename("idx_ret")
        for s, df in dd.candles.items():
            f = pd.DataFrame(index=df.index)
            c = df["close"]
            f["ret_1"] = c.pct_change(1)
            f["ret_3"] = c.pct_change(3)
            f["ret_6"] = c.pct_change(6)
            atr = _atr(df)
            f["atr_abs"] = atr
            f["atr_pct"] = atr / c
            f["rsi"] = _rsi(c)
            f["trend"] = (_rma(c, 9) - _rma(c, 21)) / c
            f["rv_20"] = c.pct_change().rolling(20).std()
            f["vwap_dev"] = (c - _session_vwap(df)) / c
            f["rel_vol"] = df["volume"] / df["volume"].rolling(20).mean()
            rng = (df["high"] - df["low"]).replace(0, np.nan)
            f["range_pos"] = (c - df["low"]) / rng
            sr = f["ret_1"] - idx_ret.reindex(f.index).values
            f["rel_strength"] = sr
            f["beta"] = (f["ret_1"].rolling(60).cov(idx_ret.reindex(f.index))
                         / idx_ret.reindex(f.index).rolling(60).var())
            self.cols[s] = f.fillna(0.0)

    def at(self, symbol, ts):
        f = self.cols[symbol]
        try:
            return f.loc[ts]
        except KeyError:
            pos = f.index.searchsorted(ts)
            return f.iloc[min(pos, len(f) - 1)]


def _chain_features(chain, spot, atr_abs, dte):
    st = chain["strikes"]
    total_ce, total_pe = float(st["ce_oi"].sum()), float(st["pe_oi"].sum())
    pcr = total_pe / total_ce if total_ce else 1.0
    call_wall = float(st.loc[st["ce_oi"].idxmax(), "strike"])
    put_wall = float(st.loc[st["pe_oi"].idxmax(), "strike"])
    atr_abs = atr_abs or (spot * 0.01)
    dist_call = (call_wall - spot) / atr_abs
    dist_put = (spot - put_wall) / atr_abs
    # ATM iv + 25d-ish skew
    i_atm = int((st["strike"] - spot).abs().idxmin())
    atm_iv = float((st.loc[i_atm, "ce_iv"] + st.loc[i_atm, "pe_iv"]) / 2)
    call_k = st.iloc[(st["strike"] - spot * 1.02).abs().idxmin()]
    put_k = st.iloc[(st["strike"] - spot * 0.98).abs().idxmin()]
    skew_25 = float(put_k["pe_iv"] - call_k["ce_iv"])
    # GEX via Black-Scholes gamma (dealer-short proxy: puts +, calls -)
    T = max(dte, 1) / 252.0
    K = st["strike"].values
    gamma_ce = _bs_gamma(spot, K, st["ce_iv"].values, T)
    gamma_pe = _bs_gamma(spot, K, st["pe_iv"].values, T)
    gex = float(np.sum(gamma_pe * st["pe_oi"].values - gamma_ce * st["ce_oi"].values)
                * spot * spot * 1e-9)
    total_oi_z = float(np.log1p(total_ce + total_pe) - 12.0)
    return {"pcr_oi": pcr, "dist_call_wall_atr": dist_call,
            "dist_put_wall_atr": dist_put, "gex": gex, "atm_iv": atm_iv,
            "skew_25": skew_25, "total_oi_z": total_oi_z}


def _bs_gamma(S, K, iv, T):
    iv = np.clip(iv, 1e-3, None)
    d1 = (np.log(S / K) + 0.5 * iv * iv * T) / (iv * np.sqrt(T))
    return norm.pdf(d1) / (S * iv * np.sqrt(T))


def _dte(ts):
    # business days to end of month (fake monthly expiry)
    eom = (ts + pd.offsets.BMonthEnd(0))
    return max(int(np.busday_count(ts.date(), eom.date())), 0)


def enrich(event, bank: FeatureBank, dd) -> dict:
    ts = pd.Timestamp(event.bar_time)
    sym = event.symbol
    row = bank.at(sym, ts)
    dstr = ts.strftime("%Y-%m-%d")
    meta = dd.meta.get((sym, dstr), {})
    chain = dd.chain.get((sym, dstr))
    dte = _dte(ts)
    f = {k: float(row.get(k, 0.0)) for k in
         ["ret_1", "ret_3", "ret_6", "atr_pct", "rsi", "trend", "rv_20",
          "vwap_dev", "rel_vol", "range_pos", "rel_strength", "beta"]}
    f["vix"] = float(meta.get("vix", dd.vix.get(dstr, 15.0)))
    atr_abs = float(row.get("atr_abs", 0.0))
    if chain is not None:
        f.update(_chain_features(chain, chain["spot"], atr_abs, dte))
    else:
        f.update({k: 0.0 for k in ("pcr_oi", "dist_call_wall_atr",
                  "dist_put_wall_atr", "gex", "atm_iv", "skew_25", "total_oi_z")})
    f["dte"] = float(dte)
    f["days_to_event"] = float(meta.get("days_to_event", 5))
    hr = ts.hour + ts.minute / 60.0
    f["tod_bucket"] = float(0 if hr < 11 else 1 if hr < 13 else 2 if hr < 14.5 else 3)
    f["dow"] = float(ts.weekday())
    # microstructure-lite (synthetic proxies)
    f["spread_ticks"] = float(1 + 3 * (meta.get("liq_tier", "A") != "A"))
    f["depth_imb"] = float(np.tanh(f["rel_strength"] * 50))
    f["dir_sign"] = 1.0 if event.direction == "BUY" else -1.0
    # categoricals
    f["strategy_id"] = event.strategy_id
    f["setup_id"] = event.setup_id
    f["era"] = meta.get("era", "pre")
    f["liq_tier"] = meta.get("liq_tier", "A")
    # non-model flags used by the filter stack (kept OUT of NUMERIC/CATEGORICAL)
    f["ban"] = 1.0 if meta.get("ban") else 0.0
    return f
