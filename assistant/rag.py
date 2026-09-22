"""assistant/rag.py — lightweight BM25 search over the project's own docs.

The assistant grounds "how does the system work / what does this term mean" on
the project's markdown docs and agent specs (docs/, docs/specs/*.json) rather
than the model's memory. BM25 is implemented inline (≈40 lines) so there is no
external dependency — the corpus is small enough that a pure-Python index over
it is instant.

Offline smoke test: ``python -m assistant.rag``.
"""
from __future__ import annotations

import math
import os
import re

_TOK = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> list[str]:
    return _TOK.findall(s.lower())


class DocIndex:
    def __init__(self, roots=("docs",), max_chunk_chars: int = 1200):
        self.chunks: list[dict] = []
        self._load(roots, max_chunk_chars)
        self._build()

    def _load(self, roots, max_chunk_chars: int) -> None:
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                for fn in sorted(files):
                    if not fn.endswith((".md", ".json", ".txt")):
                        continue
                    path = os.path.join(dirpath, fn)
                    try:
                        text = open(path, encoding="utf-8", errors="ignore").read()
                    except OSError:
                        continue
                    for chunk in self._chunk(text, max_chunk_chars):
                        self.chunks.append({"source": path, "text": chunk})

    @staticmethod
    def _chunk(text: str, maxc: int) -> list[str]:
        parts = re.split(r"\n(?=#{1,6}\s)", text)      # split on markdown headers
        out = []
        for p in parts:
            p = p.strip()
            while len(p) > maxc:
                out.append(p[:maxc])
                p = p[maxc:]
            if p:
                out.append(p)
        return out

    def _build(self) -> None:
        self.docs_toks = [_tokens(c["text"]) for c in self.chunks]
        self.df: dict[str, int] = {}
        for toks in self.docs_toks:
            for t in set(toks):
                self.df[t] = self.df.get(t, 0) + 1
        self.N = len(self.docs_toks) or 1
        self.avgdl = (sum(len(t) for t in self.docs_toks) / self.N) if self.docs_toks else 1.0

    def search(self, query: str, k: int = 4, k1: float = 1.5, b: float = 0.75) -> list[dict]:
        q = set(_tokens(query))
        scored = []
        for i, toks in enumerate(self.docs_toks):
            if not toks:
                continue
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            dl = len(toks)
            s = 0.0
            for t in q:
                if t not in tf:
                    continue
                df = self.df.get(t, 0)
                idf = math.log(1 + (self.N - df + 0.5) / (df + 0.5))
                s += idf * (tf[t] * (k1 + 1)) / (tf[t] + k1 * (1 - b + b * dl / self.avgdl))
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        out = []
        for s, i in scored[:k]:
            c = self.chunks[i]
            out.append({"source": os.path.basename(c["source"]),
                        "score": round(s, 2), "text": c["text"][:600]})
        return out


if __name__ == "__main__":
    idx = DocIndex()
    print(f"indexed {len(idx.chunks)} chunks from docs/")
    for q in ("what is the OI buildup strategy",
              "how does the Rbknox swing signal work",
              "black scholes implied volatility kernel"):
        hits = idx.search(q, k=2)
        print(f"\nQ: {q}")
        for h in hits:
            print(f"  [{h['score']}] {h['source']}: {h['text'][:80].strip()}…")
    assert idx.chunks, "no docs indexed"
    print("\nrag smoke test: OK")
