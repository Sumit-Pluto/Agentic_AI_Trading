"""feed/pump.py — binds the proxy-aware ShoonyaFeed to a TickCache.

Division of labour:

* ``shoonya_client.ShoonyaFeed`` owns the **transport** — the websocket
  connect/subscribe/proxy/self-heal. Untouched.
* ``FeedPump`` owns the **consumption model** (adopted from the Snowball
  system):
    - :meth:`on_tick` merges every wire frame into a freshness-aware
      :class:`~feed.cache.TickCache` (this is what the scanner/DataHub read).
    - an optional background *keeper* thread REST-polls the tokens the socket
      has gone quiet on, so cached prices keep advancing during a partial feed
      stall (Snowball's ``_price_keeper_loop``, re-cast to this project's thread
      model instead of asyncio).

Thread model: ``ShoonyaFeed`` dispatches ``on_tick`` on its socket thread; the
keeper runs on its own daemon thread; DataHub/scanner read the cache from the
scanner thread. All shared state lives in the locked :class:`TickCache`, so
:meth:`on_tick` never blocks the socket thread.

Offline smoke test: ``python -m feed.pump``.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Iterable, Optional

from feed.cache import TickCache

log = logging.getLogger("feed.pump")


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class FeedPump:
    """Feeds a :class:`TickCache` from ``ShoonyaFeed`` ticks, with an optional
    REST stale-fallback keeper.

    Parameters
    ----------
    cache:
        The :class:`TickCache` to populate (a fresh one is created if omitted).
    quote_fn:
        Optional ``(exchange, token) -> quote_dict | None`` used by the keeper
        to REST-refresh stale tokens (e.g. ``DataHub._quote``). If ``None`` the
        keeper is a no-op — reads simply fall back to DataHub's own on-demand
        REST path.
    keeper_interval:
        Seconds between keeper sweeps.
    stale_after:
        A tracked token untouched by the socket for longer than this (seconds)
        is REST-refreshed by the keeper.
    max_polls_per_sweep:
        Cap on REST calls per keeper sweep, so a mass stall can't burst the
        broker rate limit.
    """

    def __init__(self, cache: Optional[TickCache] = None, *,
                 quote_fn: Optional[Callable[[str, str], Optional[dict]]] = None,
                 keeper_interval: float = 1.0, stale_after: float = 3.0,
                 max_polls_per_sweep: int = 8) -> None:
        self.cache = cache if cache is not None else TickCache()
        self._quote_fn = quote_fn
        self.keeper_interval = keeper_interval
        self.stale_after = stale_after
        self.max_polls_per_sweep = max_polls_per_sweep
        self._subs: set[str] = set()          # "EXCH|TOKEN" we expect ticks for
        self._subs_lock = threading.Lock()
        self._stop = threading.Event()
        self._keeper: Optional[threading.Thread] = None

    # -- websocket callback --------------------------------------------------

    def on_tick(self, msg: dict, snap: Optional[dict] = None) -> None:
        """``ShoonyaFeed`` ``on_tick`` handler — merge one frame into the cache.

        ``snap`` (ShoonyaFeed's own merged dict) is ignored; the cache does its
        own freshness-stamped merge. Must never block: it runs on the socket
        thread.
        """
        try:
            self.cache.apply(msg)
        except Exception as e:            # never let a bad frame kill the socket
            log.debug("on_tick apply failed: %s", e)

    # -- subscription bookkeeping (so the keeper knows what to watch) --------

    def track(self, keys: Iterable[str]) -> None:
        with self._subs_lock:
            self._subs.update(keys)

    def untrack(self, keys: Iterable[str]) -> None:
        with self._subs_lock:
            self._subs.difference_update(keys)

    def tracked(self) -> set[str]:
        with self._subs_lock:
            return set(self._subs)

    # -- optional REST keeper ------------------------------------------------

    def start_keeper(self) -> None:
        if self._quote_fn is None or self._keeper is not None:
            return
        self._stop.clear()
        self._keeper = threading.Thread(target=self._keeper_loop,
                                        name="feed-keeper", daemon=True)
        self._keeper.start()
        log.info("feed keeper started (interval=%.1fs, stale_after=%.1fs)",
                 self.keeper_interval, self.stale_after)

    def stop(self) -> None:
        self._stop.set()

    def _keeper_loop(self) -> None:
        assert self._quote_fn is not None
        while not self._stop.wait(self.keeper_interval):
            try:
                stale = self.cache.stale_keys(self.stale_after, only=self.tracked())
                for key in stale[:self.max_polls_per_sweep]:
                    exch, _, tok = key.partition("|")
                    if not tok:
                        continue
                    q = self._quote_fn(exch, tok)
                    if not q:
                        continue
                    self.cache.apply_quote(
                        key, lp=_f(q.get("lp")), bid=_f(q.get("bp1")),
                        ask=_f(q.get("sp1")), source="poll")
            except Exception as e:
                log.debug("keeper sweep error: %s", e)


if __name__ == "__main__":
    # Offline smoke test — no network. Run: python -m feed.pump
    import time as _t

    pump = FeedPump()
    pump.track(["NSE|26000"])

    # A websocket frame arrives -> cache reflects it.
    pump.on_tick({"t": "tf", "e": "NSE", "tk": "26000", "lp": "101.5"})
    assert pump.cache.lp("NSE|26000") == 101.5

    # Keeper: simulate a stall + a REST quote_fn that returns a fresher price.
    polls: list[tuple] = []

    def fake_quote(exch, tok):
        polls.append((exch, tok))
        return {"lp": "102.0", "bp1": "101.9", "sp1": "102.1"}

    kp = FeedPump(quote_fn=fake_quote, keeper_interval=0.05,
                  stale_after=0.0, max_polls_per_sweep=4)
    kp.track(["NSE|26000", "MCX|432556"])
    kp.start_keeper()
    _t.sleep(0.2)
    kp.stop()
    assert polls, "keeper should have polled stale tracked tokens"
    assert kp.cache.lp("NSE|26000") == 102.0
    assert kp.cache.get("NSE|26000").source == "poll"

    print("feed.pump smoke test: OK", f"({len(polls)} keeper polls)")
