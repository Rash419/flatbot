"""Small text helpers shared by the bot and the AI extractor.

Bank SMS are read by the AI (app/extract.py), which returns a `Txn`. This module only holds that
type, the key used for per-merchant always-split / always-skip rules, and the parser for spends
typed to the bot by hand ("450 groceries").
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Txn:
    amount: int  # paise
    merchant: str | None
    ref: str | None


def merchant_key(name: str) -> str:
    """Stable key for remembering a per-merchant rule."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


_NUM = r"(\d[\d,]*(?:\.\d{1,2})?)"
_CUR = r"(?:rs\.?|inr|₹)"
_MANUAL_LEAD = re.compile(rf"^{_CUR}?\s*{_NUM}\s+(.+)$", re.I | re.S)
_MANUAL_TRAIL = re.compile(rf"^(.+?)\s+{_CUR}?\s*{_NUM}$", re.I | re.S)


def _to_paise(num: str) -> int:
    whole, _, frac = num.replace(",", "").partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def parse_manual(text: str) -> tuple[int, str] | None:
    """'450 groceries' or 'groceries 450' -> (paise, description)."""
    text = " ".join(text.split())
    m = _MANUAL_LEAD.match(text)
    if m:
        amount, desc = _to_paise(m.group(1)), m.group(2)
    else:
        m = _MANUAL_TRAIL.match(text)
        if not m:
            return None
        amount, desc = _to_paise(m.group(2)), m.group(1)
    desc = desc.strip(" -:,")
    if amount <= 0 or not desc:
        return None
    return amount, desc[:80]
