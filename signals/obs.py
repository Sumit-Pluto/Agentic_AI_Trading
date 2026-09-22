"""Python port of indicators/OBS.txt ("Buy the dips - sell the tops").

Faithful reproduction of the Pine v5 state machine:

  * RSI(14) keeps a persistent memory of the LAST extreme it visited
    (lastRSI = "Overbought" / "Oversold").
  * After an Overbought episode (strength), the script waits for Williams
    %R(24) to dip under -80 → upTrend regime ("buy the dips" mode).
    After an Oversold episode (weakness), %R over -20 → downTrend regime
    ("sell the tops" mode).
  * BUY triangle  = upTrend regime AND %R crosses UP through -80.
    SELL triangle = downTrend regime AND %R crosses DOWN through -20.
  * EXIT cross    = RSI reaching the opposite extreme cancels the regime.

evaluate() walks the whole candle history deterministically (matches how
Pine executes bar by bar) and reports the state/events of the last CLOSED
bar. ~300 bars per stock → negligible cost.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .indicators import rsi, williams_r


@dataclass
class ObsState:
    buy_triangle: bool          # "Buy the dip" fired on the last closed bar
    sell_triangle: bool         # "Sell the top" fired on the last closed bar
    up_regime: bool             # currently in "buy the dips" mode
    down_regime: bool           # currently in "sell the tops" mode
    exit_cross: bool            # regime cancelled on this bar
    rsi: float
    percent_r: float
    last_rsi_state: str         # "Overbought" / "Oversold" / "neutral"
    # label EVENTS — named EXACTLY as the Pine text (OBS.txt lines 70-76)
    # so the name can never lie again:
    #   label_sell = green "Look to sell" ABOVE bar at tops
    #                (RSI crosses DOWN out of overbought)  → SELL idea
    #   label_buy  = red   "Look to buy"  BELOW bar at bottoms
    #                (RSI crosses UP out of oversold)      → BUY idea
    label_sell: bool = False
    label_buy: bool = False


def evaluate_series(df: pd.DataFrame, rsi_len: int = 14, rsi_os: float = 30.0,
                    rsi_ob: float = 70.0, wpr_len: int = 24) -> pd.DataFrame:
    """Single-pass replay variant: same state machine as evaluate(), but
    records the triangle events of EVERY bar. Returns DataFrame(buy_tri,
    sell_tri) aligned to df.index — used by the day simulator, where calling
    evaluate() per prefix would be O(n²)."""
    r = rsi(df["close"], rsi_len).values
    wpr = williams_r(df, wpr_len).values

    last_rsi = "neutral"
    up_trend = down_trend = False
    n = len(df)
    buy = [False] * n
    sell = [False] * n
    lbl_buy = [False] * n           # red "Look to buy" (RSI exits oversold)
    lbl_sell = [False] * n          # green "Look to sell" (exits overbought)
    for i in range(n):
        ri, wi = r[i], wpr[i]
        if pd.isna(ri) or pd.isna(wi):
            continue
        r_prev = r[i - 1] if i > 0 else float("nan")
        if not pd.isna(r_prev):
            if ri >= rsi_os and r_prev < rsi_os:
                lbl_buy[i] = True
            if ri <= rsi_ob and r_prev > rsi_ob:
                lbl_sell[i] = True
        if ri < rsi_os:
            last_rsi = "Oversold"
        elif ri > rsi_ob:
            last_rsi = "Overbought"
        if last_rsi == "Overbought" and wi < -80:
            up_trend, down_trend = True, False
        elif last_rsi == "Oversold" and wi > -20:
            up_trend, down_trend = False, True
        w_prev = wpr[i - 1] if i > 0 else float("nan")
        if up_trend and (not pd.isna(w_prev)) and w_prev < -80 and wi > -80:
            buy[i] = True
        if down_trend and (not pd.isna(w_prev)) and w_prev > -20 and wi < -20:
            sell[i] = True
        if ri >= rsi_ob and down_trend:
            down_trend = False
        elif ri <= rsi_os and up_trend:
            up_trend = False
    return pd.DataFrame({"buy_tri": buy, "sell_tri": sell,
                         "label_buy": lbl_buy,
                         "label_sell": lbl_sell}, index=df.index)


def evaluate(df: pd.DataFrame, rsi_len: int = 14, rsi_os: float = 30.0,
             rsi_ob: float = 70.0, wpr_len: int = 24) -> ObsState:
    r = rsi(df["close"], rsi_len).values
    wpr = williams_r(df, wpr_len).values

    last_rsi = "neutral"
    up_trend = False
    down_trend = False
    buy_tri = sell_tri = exit_cross = False
    lbl_buy = lbl_sell = False

    n = len(df)
    for i in range(n):
        buy_tri = sell_tri = exit_cross = False
        lbl_buy = lbl_sell = False
        ri, wi = r[i], wpr[i]
        if pd.isna(ri) or pd.isna(wi):
            continue

        # RSI extreme memory (Pine: if rsi < OS ... else if rsi > OB)
        if ri < rsi_os:
            last_rsi = "Oversold"
        elif ri > rsi_ob:
            last_rsi = "Overbought"

        # regime engagement
        if last_rsi == "Overbought" and wi < -80:
            up_trend, down_trend = True, False
        elif last_rsi == "Oversold" and wi > -20:
            up_trend, down_trend = False, True

        # %R re-entry crosses
        w_prev = wpr[i - 1] if i > 0 else float("nan")
        wpr_ob_to_normal = (not pd.isna(w_prev)) and w_prev > -20 and wi < -20
        wpr_os_to_normal = (not pd.isna(w_prev)) and w_prev < -80 and wi > -80

        # triangles (the actual signals)
        if up_trend and wpr_os_to_normal:
            buy_tri = True
        if down_trend and wpr_ob_to_normal:
            sell_tri = True

        # label EVENTS (Pine lines 70-76): RSI re-entering from an extreme.
        # exits oversold -> red "Look to buy"; exits overbought -> green
        # "Look to sell" (names match the on-chart text verbatim)
        r_prev = r[i - 1] if i > 0 else float("nan")
        lbl_buy = ((not pd.isna(r_prev))
                   and ri >= rsi_os and r_prev < rsi_os)
        lbl_sell = ((not pd.isna(r_prev))
                    and ri <= rsi_ob and r_prev > rsi_ob)

        # EXIT: RSI reaches the opposite extreme → cancel regime
        if ri >= rsi_ob and down_trend:
            exit_cross, down_trend = True, False
        elif ri <= rsi_os and up_trend:
            exit_cross, up_trend = True, False

    return ObsState(buy_triangle=buy_tri, sell_triangle=sell_tri,
                    up_regime=up_trend, down_regime=down_trend,
                    exit_cross=exit_cross,
                    rsi=float(r[-1]) if n else float("nan"),
                    percent_r=float(wpr[-1]) if n else float("nan"),
                    last_rsi_state=last_rsi,
                    label_buy=bool(lbl_buy),
                    label_sell=bool(lbl_sell))
