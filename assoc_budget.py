"""Conservative local reservation for the optional recall work only."""
from contextlib import closing
import datetime as dt
import os
import sqlite3

class BudgetExceeded(RuntimeError):
    pass

def reserve(db_path, *, ceiling_usd, text, output=0, embedding=False, retries=1):
    # UTF-8 bytes bound uncached prompt tokens for the deployed tokenizers.
    # Microdollars/token, rounded above the verified configured provider prices.
    input_rate, output_rate = ((.05,0) if embedding else (.08,.22))
    amount = max(1, int(((len(text.encode("utf-8"))+240)*input_rate
                        + output*output_rate)*retries)+1)
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    with closing(sqlite3.connect(db_path, timeout=.08)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS assoc_budget(month TEXT PRIMARY KEY, microdollars INTEGER NOT NULL)")
        db.execute("INSERT OR IGNORE INTO assoc_budget VALUES(?,0)",(month,))
        updated = db.execute("UPDATE assoc_budget SET microdollars=microdollars+? WHERE month=? AND microdollars+?<=?",
                             (amount,month,amount,int(ceiling_usd*1_000_000)))
        if not updated.rowcount:
            raise BudgetExceeded("optional recall monthly allowance exhausted")
