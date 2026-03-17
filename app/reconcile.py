import asyncpg

BALANCE_VS_ENTRIES = """
SELECT a.id, a.balance_minor, COALESCE(SUM(e.amount_minor), 0) AS ledger_sum
FROM accounts a
LEFT JOIN ledger_entries e ON e.account_id = a.id
GROUP BY a.id
HAVING a.balance_minor <> COALESCE(SUM(e.amount_minor), 0)
"""

# A succeeded transfer has exactly two entries that cancel out. A failed one has none.
UNBALANCED_TRANSFERS = """
SELECT t.id, t.status, COUNT(e.id) AS entry_count, COALESCE(SUM(e.amount_minor), 0) AS entry_sum
FROM transfers t
LEFT JOIN ledger_entries e ON e.transfer_id = t.id
GROUP BY t.id
HAVING COALESCE(SUM(e.amount_minor), 0) <> 0
    OR (t.status = 'SUCCEEDED' AND COUNT(e.id) <> 2)
    OR (t.status <> 'SUCCEEDED' AND COUNT(e.id) <> 0)
"""

# Money only moves between accounts, so each currency must net to zero.
CURRENCY_TOTALS = """
SELECT currency, SUM(balance_minor) AS total
FROM accounts
GROUP BY currency
HAVING SUM(balance_minor) <> 0
"""


async def reconcile(pool: asyncpg.Pool) -> list[dict]:
    """Return every place where cached balances and the ledger disagree."""
    mismatches = []
    async with pool.acquire() as conn:
        # One snapshot for all three checks, so transfers committing
        # mid-check cannot cause a false alarm.
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            for row in await conn.fetch(BALANCE_VS_ENTRIES):
                mismatches.append(
                    {
                        "kind": "balance_mismatch",
                        "account_id": str(row["id"]),
                        "balance_minor": row["balance_minor"],
                        "ledger_sum": row["ledger_sum"],
                    }
                )
            for row in await conn.fetch(UNBALANCED_TRANSFERS):
                mismatches.append(
                    {
                        "kind": "unbalanced_transfer",
                        "transfer_id": str(row["id"]),
                        "status": row["status"],
                        "entry_count": row["entry_count"],
                        "entry_sum": row["entry_sum"],
                    }
                )
            for row in await conn.fetch(CURRENCY_TOTALS):
                mismatches.append(
                    {"kind": "currency_imbalance", "currency": row["currency"], "total": row["total"]}
                )
    return mismatches
