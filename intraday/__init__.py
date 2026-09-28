"""Intraday agentic options-trading engine.

A port of the swing_hyena agentic spine to an intraday cadence, feeding off the
broker Gateway (./gateway) for Shoonya market data + orders and computing option
greeks/IV locally (Black-Scholes). See docs and the plan in the repo root.

Design invariant (inherited from swing_hyena): SINGLE CODE PATH —
backtest == paper == live. Only the Broker implementation and the data source
(IntradayContext) change; the scanner, agents, risk, rules and exits are
byte-for-byte identical across all three modes.

PDF module map (Agentic Trading System — Phase 1):
  §2.1 Money & Risk           -> intraday.risk.governor
  §2.2 Order Management       -> intraday.orders.manager (+ intraday.brokers)
  §2.3 Rule Engine            -> intraday.rules.engine (+ Gateway ExecutionCoordinator, §8)
  §2.4 AI Market Intelligence -> intraday.agents + intraday.intelligence
  §2.5 Trade Analysis/Learning-> intraday.journal + intraday.training
  §4   Order DB               -> intraday.journal.store (SQLite)
  §6   Local LLM (optional)   -> intraday.llm (interface + template fallback, OFF)
  §7   Latency <5s            -> intraday.orders.manager latency stamps
"""
