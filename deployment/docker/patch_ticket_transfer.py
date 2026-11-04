#!/usr/bin/env python3
"""Compatibility shim for pretix-ticket-transfer on newer pretix.

OrderChangeManager._payment_fee_diff() was removed; payment fees are
recalculated in _recalculate_rounding_total_and_payment_fee(). Add a
no-op if missing.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CANDIDATES = [
    Path("/pretix/local/pretix-ticket-transfer/pretix_ticket_transfer/user_split.py"),
    *Path("/usr/local/lib").glob("python*/site-packages/pretix_ticket_transfer/user_split.py"),
]
PATTERN = re.compile(
    r"(class TicketTransferChangeManager\(OrderChangeManager\):.*?)(\n    def commit\(self, check_quotas=True\):)",
    flags=re.DOTALL,
)
REPLACEMENT = (
    r'\1\n    def _payment_fee_diff(self):\n'
    r'        """No-op method - payment fees are recalculated in _recalculate_total_and_payment_fee()"""\n'
    r'        pass\2'
)


def main() -> int:
    for path in CANDIDATES:
        if not path.exists():
            continue
        text = path.read_text()
        if "def _payment_fee_diff(self):" in text:
            continue
        new = PATTERN.sub(REPLACEMENT, text, count=1)
        if new != text:
            path.write_text(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
