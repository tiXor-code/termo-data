"""Append-only slug registry (registry/slugs.json - committed, never reassigned).

Two namespaces share one slug space. PT identity = canonical pt_norm; street
identity = f"{street_type}|{street_norm}" (matches the db PK on
episode_street/street_pt; sector is NOT identity - streets span sectors).
Lookup precedes computation: once an identity is registered its slug is
returned untouched, so committed slugs survive future slugify-rule changes.
Mutating or deleting an entry is impossible through this API.

Determinism: callers must register identities in sorted order (publish.py
does), so first-registration collision tie-breaks are stable across runs.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path


def slugify(text: str) -> str:
    """NFKD diacritic fold, lowercase, ASCII, non-alnum -> hyphen, collapse."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def pt_slug_base(pt_norm: str) -> str:
    s = slugify(pt_norm)
    return f"pt-{s}" if s else "pt"


def street_slug_base(stype: str, snorm: str) -> str:
    # Empty street_type (593 db links) yields no prefix: ("", "13 septembrie")
    # -> "13-septembrie". The contract allows it; never collides with pt-*
    # in practice, and publish registers PTs first so any clash gets suffixed.
    return slugify(f"{stype} {snorm}".strip()) or "strada"


class SlugRegistry:
    def __init__(self, pt: dict[str, str] | None = None,
                 street: dict[str, str] | None = None):
        self.pt: dict[str, str] = dict(pt or {})
        self.street: dict[str, str] = dict(street or {})
        self._taken = set(self.pt.values()) | set(self.street.values())
        if len(self._taken) != len(self.pt) + len(self.street):
            raise ValueError("slug registry corrupt: a slug maps from two identities")
        self.dirty = False

    @classmethod
    def load(cls, path: str | Path) -> "SlugRegistry":
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise ValueError(f"unsupported slug registry version: {data.get('version')!r}")
        return cls(data.get("pt"), data.get("street"))

    def ensure_pt(self, pt_norm: str) -> str:
        got = self.pt.get(pt_norm)
        if got is not None:
            return got
        slug = self._claim(pt_slug_base(pt_norm), sectors=())
        self.pt[pt_norm] = slug
        self.dirty = True
        return slug

    def ensure_street(self, stype: str, snorm: str, sectors: list[int] | tuple) -> str:
        ident = f"{stype}|{snorm}"
        got = self.street.get(ident)
        if got is not None:
            return got
        slug = self._claim(street_slug_base(stype, snorm), sectors=sectors)
        self.street[ident] = slug
        self.dirty = True
        return slug

    def _claim(self, base: str, sectors) -> str:
        """Collision with a slug owned by a different identity: streets try
        -sector-<min> first (per ARTIFACTS), then -2, -3...; PTs go straight
        to -2, -3..."""
        if base not in self._taken:
            self._taken.add(base)
            return base
        if sectors:
            cand = f"{base}-sector-{min(sectors)}"
            if cand not in self._taken:
                self._taken.add(cand)
                return cand
        i = 2
        while f"{base}-{i}" in self._taken:
            i += 1
        cand = f"{base}-{i}"
        self._taken.add(cand)
        return cand

    def save(self, path: str | Path) -> bool:
        """Write only if dirty; sorted keys, stable bytes. Returns changed."""
        if not self.dirty:
            return False
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1,
                   "pt": dict(sorted(self.pt.items())),
                   "street": dict(sorted(self.street.items()))}
        p.write_text(json.dumps(payload, indent=1, ensure_ascii=True) + "\n",
                     encoding="utf-8")
        self.dirty = False
        return True
