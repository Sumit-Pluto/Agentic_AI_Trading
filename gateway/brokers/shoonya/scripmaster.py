"""
Scripmaster CSV loader for symbol-to-token mapping.

This module provides optional support for loading a local scripmaster CSV file
for faster symbol-to-token lookups without API calls.

Usage:
    1. Download scripmaster.csv from Shoonya
    2. Place in brokers/shoonya/ folder
    3. Use ScriptMaster class to load and lookup symbols
"""

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple

from rapidfuzz import fuzz, process

# Fuzzy fallback tuning: plain (non-partial) ratio avoids short substrings
# (e.g. "bi") scoring artificially high against a longer query like "sbinn".
_FUZZY_MIN_QUERY_LEN = 3
_FUZZY_SCORE_CUTOFF = 75
_FUZZY_MAX_SYM_MATCHES = 6

# Compound "symbol+expiry month" queries (e.g. "niftyjuly", "goldjuly") never
# appear as a contiguous substring in tsym, since the real format interleaves
# a day/year between them (NIFTY28JUL26F, GOLD30APR27P176500). So a trailing
# month name/abbreviation is split off and matched separately against tsym.
_MONTH_ABBR = {
    "jan": "jan", "january": "jan",
    "feb": "feb", "february": "feb",
    "mar": "mar", "march": "mar",
    "apr": "apr", "april": "apr",
    "may": "may",
    "jun": "jun", "june": "jun",
    "jul": "jul", "july": "jul",
    "aug": "aug", "august": "aug",
    "sep": "sep", "sept": "sep", "september": "sep",
    "oct": "oct", "october": "oct",
    "nov": "nov", "november": "nov",
    "dec": "dec", "december": "dec",
}
_MONTH_TOKENS_BY_LEN = sorted(_MONTH_ABBR, key=len, reverse=True)


def _parse_expd(s: str) -> Optional[datetime]:
    """Parse a scripmaster expiry ("31-JUL-2026") into a datetime; None on failure.

    strptime with %b is case-insensitive, so both "31-JUL-2026" and
    "31-Jul-2026" parse. This is the same format used across the codebase
    (market search sort, option-chain expiry list).
    """
    try:
        return datetime.strptime(s, "%d-%b-%Y")
    except (ValueError, TypeError):
        return None


def _split_month_suffix(search_lower: str) -> Optional[Tuple[str, str]]:
    """Split "niftyjuly" -> ("nifty", "jul"). Only a trailing month token
    counts, and the remaining base must be at least 2 chars, to avoid
    misfiring on plain symbol names that just happen to end similarly."""
    for token in _MONTH_TOKENS_BY_LEN:
        if search_lower.endswith(token):
            base = search_lower[: -len(token)]
            if len(base) >= 2:
                return base, _MONTH_ABBR[token]
    return None


class ScriptMaster:
    """Load and query the scripmaster CSV for symbol-to-token mapping."""

    def __init__(self, csv_path: Optional[Path] = None):
        """
        Initialize ScriptMaster.

        Args:
            csv_path: Path to scripmaster.csv. If None, defaults to
                     brokers/shoonya/scripmaster.csv
        """
        if csv_path is None:
            csv_path = Path(__file__).parent / "scripmaster.csv"

        self.csv_path = Path(csv_path)
        self.master: Dict[str, Dict[str, str]] = {}  # {exchange|symbol: {data}}
        self.token_cache: Dict[Tuple[str, str], str] = {}  # {(exchange, symbol): token}
        self.sym_index: Dict[str, List[Dict[str, str]]] = {}  # {sym_lower: [rows]}
        self._all_syms_lower: List[str] = []
        self._syms_lower_by_exchange: Dict[str, List[str]] = {}

        if self.csv_path.exists():
            self._load_csv()

    def _load_csv(self) -> None:
        """Load and parse the scripmaster CSV file."""
        try:
            exch_syms: Dict[str, set] = {}
            with open(self.csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if not row:
                        continue

                    # Typical columns: exch, tsym, token, ...
                    exchange = row.get("exch", "").strip()
                    symbol = row.get("tsym", "").strip()
                    token = row.get("token", "").strip()

                    if exchange and symbol and token:
                        key = f"{exchange}|{symbol}"
                        self.master[key] = row
                        self.token_cache[(exchange, symbol)] = token

                        sym_lower = row.get("sym", "").strip().lower()
                        if sym_lower:
                            self.sym_index.setdefault(sym_lower, []).append(row)
                            exch_syms.setdefault(exchange, set()).add(sym_lower)

            self._all_syms_lower = list(self.sym_index.keys())
            self._syms_lower_by_exchange = {e: list(s) for e, s in exch_syms.items()}

            print(f"✓ Loaded {len(self.master)} symbols from {self.csv_path}")
        except Exception as e:
            print(f"⚠ Failed to load scripmaster.csv: {e}")

    def get_token(self, exchange: str, symbol: str) -> Optional[str]:
        """
        Look up token for a symbol.

        Args:
            exchange: Exchange code (e.g., "NSE")
            symbol: Trading symbol (e.g., "RELIANCE-EQ")

        Returns:
            Token number or None if not found
        """
        return self.token_cache.get((exchange, symbol))

    def next_expiries(self, exchange: str, tsym: str) -> List[Dict[str, str]]:
        """Future contracts of the SAME underlying + instrument type as
        (exchange, tsym), whose expiry is strictly AFTER the current contract's,
        ordered nearest-first.

        Returns [] when the contract is unknown, has no parseable expiry, or is
        already the last listed expiry. This is the rollover resolver: the near
        contract (e.g. LEAD31JUL26) maps to [LEAD31AUG26, LEAD30SEP26, ...].

        Matching on `sym` keeps distinct underlyings apart (LEAD never pulls in
        LEADMINI). Matching the current contract's own `instrumenttype` +
        `opttype` keeps futures away from options and generically covers
        FUTCOM / FUTIDX / FUTSTK / FUTCUR.
        """
        cur = self.master.get(f"{exchange}|{tsym}")
        if not cur:
            return []
        cur_exp = _parse_expd(cur.get("expd", ""))
        if cur_exp is None:
            return []

        sym = cur.get("sym", "").strip().lower()
        instr = cur.get("instrumenttype", "")
        opt = cur.get("opttype", "") or ""

        matches: List[Tuple[datetime, Dict[str, str]]] = []
        for row in self.sym_index.get(sym, []):
            if row.get("exch", "") != exchange:
                continue
            if row.get("instrumenttype", "") != instr:
                continue
            if (row.get("opttype", "") or "") != opt:
                continue
            exp = _parse_expd(row.get("expd", ""))
            if exp is None or exp <= cur_exp:
                continue
            matches.append((exp, row))

        matches.sort(key=lambda t: t[0])
        return [row for _exp, row in matches]

    def search(self, exchange: str, search_text: str, fuzzy: bool = True) -> list[Dict[str, str]]:
        """
        Search for symbols matching search_text against tsym and sym (underlying).
        If exchange is empty string, searches across all exchanges.

        Compound queries like "niftyjuly" or "goldjuly" (symbol + expiry
        month, no separator) are matched by splitting off the trailing month
        token and requiring it separately in tsym, since real contract codes
        interleave a day/year between the symbol and month.

        When fuzzy is enabled, also runs a typo-tolerant fallback pass against
        unique underlying symbols (e.g. "relaince" still surfaces RELIANCE).
        Fuzzy hits are appended after substring hits and tagged with
        row["_fuzzy"] = True so callers can rank them lower.
        """
        results = []
        seen_keys = set()
        search_lower = search_text.lower()

        # Match each whitespace-separated token independently and require them
        # all, so "rel 1600" (symbol root + strike) finds RELIANCE...1600 even
        # though that string never appears contiguously — the space is not in
        # the contract code. A strike like "1600" lives only in the tsym while
        # "rel" is in both tsym and sym, so we test each token against either.
        # A single-token query reduces to the previous substring behaviour.
        tokens = search_lower.split()

        for key, row in self.master.items():
            if exchange and not key.startswith(f"{exchange}|"):
                continue

            tsym = row.get("tsym", "").lower()
            sym = row.get("sym", "").lower()
            if tokens and all(tok in tsym or tok in sym for tok in tokens):
                results.append(row)
                seen_keys.add(key)

        month_split = _split_month_suffix(search_lower)
        if month_split:
            base, month_abbr = month_split
            for key, row in self.master.items():
                if key in seen_keys:
                    continue
                if exchange and not key.startswith(f"{exchange}|"):
                    continue
                tsym = row.get("tsym", "").lower()
                sym = row.get("sym", "").lower()
                if (base in tsym or base in sym) and month_abbr in tsym:
                    results.append(row)
                    seen_keys.add(key)

        # Typo tolerance only makes sense for a single-token query (e.g.
        # "relaince" -> RELIANCE). On a multi-token refinement like
        # "reliance 1600" the whole string still fuzzy-matches "reliance"
        # above the cutoff and would drag in every strike, defeating the
        # "1600" filter — so the fuzzy pass is skipped once there's a space.
        if fuzzy and len(tokens) == 1 and len(search_lower) >= _FUZZY_MIN_QUERY_LEN:
            candidates = (
                self._syms_lower_by_exchange.get(exchange, [])
                if exchange
                else self._all_syms_lower
            )
            matches = process.extract(
                search_lower,
                candidates,
                scorer=fuzz.ratio,
                limit=_FUZZY_MAX_SYM_MATCHES,
                score_cutoff=_FUZZY_SCORE_CUTOFF,
            )
            for matched_sym, _score, _idx in matches:
                for row in self.sym_index.get(matched_sym, []):
                    key = f"{row.get('exch', '')}|{row.get('tsym', '')}"
                    if key in seen_keys:
                        continue
                    if exchange and row.get("exch", "") != exchange:
                        continue
                    fuzzy_row = dict(row)
                    fuzzy_row["_fuzzy"] = True
                    results.append(fuzzy_row)
                    seen_keys.add(key)

        return results

    def is_loaded(self) -> bool:
        """Check if scripmaster was successfully loaded."""
        return len(self.master) > 0


# Global instance (lazy-loaded)
_instance: Optional[ScriptMaster] = None


def get_scripmaster() -> ScriptMaster:
    """Get or create the global ScriptMaster instance."""
    global _instance
    if _instance is None:
        _instance = ScriptMaster()
    return _instance


def reload_scripmaster() -> ScriptMaster:
    """Force a fresh reload of the scripmaster CSV into memory.

    Call this after re-downloading scripmaster.csv so the running process picks
    up new/expired contracts without a restart (the global instance is otherwise
    cached for the process lifetime).
    """
    global _instance
    _instance = ScriptMaster()
    return _instance
