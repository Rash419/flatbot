"""Pure ledger arithmetic. All money is integer paise (or cents)."""

from __future__ import annotations

from collections.abc import Iterable


def split_equal(amount: int, members: list[int], payer: int) -> dict[int, int]:
    """Equal shares; the paise that do not divide evenly go to the payer's own share."""
    base, extra = divmod(amount, len(members))
    shares = {m: base for m in members}
    shares[payer if payer in shares else members[0]] += extra
    return shares


def shares_for(kind: str, amount: int, payer: int, members: list[int],
               counterparty: int | None = None) -> dict[int, int]:
    """Who owes what on one shared item. `full` and `settle` put it all on the counterparty."""
    if kind == "split":
        return split_equal(amount, members, payer)
    if kind in ("full", "settle") and counterparty is not None and counterparty != payer:
        return {counterparty: amount}
    raise ValueError(f"bad share kind {kind!r}")


def proportional_part(full: int, ticked: int, whole: int) -> int:
    """full × ticked ÷ whole, rounded half up, so fees, taxes and discounts spread proportionally."""
    if whole <= 0 or not 0 <= ticked <= whole:
        raise ValueError("ticked must be within 0..whole and whole positive")
    return (2 * full * ticked + whole) // (2 * whole)


def nets(debts: Iterable[tuple[int, int, int]]) -> dict[int, int]:
    """Net per member from (debtor, creditor, amount) rows: positive means others owe them.

    Each row is one member's share of an item someone else paid, so this equals
    "paid on shared items minus own shares" without the self-shares cancelling out.
    """
    out: dict[int, int] = {}
    for debtor, creditor, amount in debts:
        out[debtor] = out.get(debtor, 0) - amount
        out[creditor] = out.get(creditor, 0) + amount
    return out


def pairwise(debts: Iterable[tuple[int, int, int]], me: int, other: int) -> int:
    """What `other` owes `me` across all shared items (negative: I owe them)."""
    total = 0
    for debtor, creditor, amount in debts:
        if debtor == other and creditor == me:
            total += amount
        elif debtor == me and creditor == other:
            total -= amount
    return total


def settle_up(net: dict[int, int]) -> list[tuple[int, int, int]]:
    """Greedy "who pays whom": (debtor, creditor, amount), largest balances first."""
    debtors = sorted(((-v, k) for k, v in net.items() if v < 0), key=lambda p: (-p[0], p[1]))
    creditors = sorted(((v, k) for k, v in net.items() if v > 0), key=lambda p: (-p[0], p[1]))
    out = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        owe, debtor = debtors[i]
        due, creditor = creditors[j]
        pay = min(owe, due)
        out.append((debtor, creditor, pay))
        debtors[i] = (owe - pay, debtor)
        creditors[j] = (due - pay, creditor)
        if owe == pay:
            i += 1
        if due == pay:
            j += 1
    return out


def _group(whole: int, indian: bool) -> str:
    digits = str(whole)
    if not indian or len(digits) <= 3:
        return f"{whole:,}"
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    parts.insert(0, head)
    return ",".join(parts) + "," + tail


def money(paise: int, currency: str = "INR") -> str:
    """₹1,234.50 / ₹450 / ₹1,00,000 for INR; 1,234.50 USD otherwise."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    inr = currency.upper() == "INR"
    text = _group(whole, inr) + (f".{frac:02d}" if frac else "")
    return f"{sign}₹{text}" if inr else f"{sign}{text} {currency.upper()}"


def plain_amount(paise: int) -> str:
    """Machine-friendly decimal for CSV: 1234.50"""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    return f"{sign}{whole}.{frac:02d}"
