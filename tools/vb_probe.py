"""
DHYAAN VOICE BRAIN — startup probe.
Called by start_voicebrain.ps1 to report two things in plain, easy-to-parse
lines (so PowerShell never has to embed Python and fight its quoting):

  DB=online                      | DB=OFFLINE: <reason>
  WALLET=<float>|OK=<bool>|ERR=<reason>

Read-only. Reuses the project's own config + the Dialer's balance reader
(never forks). voicebrain.* only — it just opens and closes a connection.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def check_db():
    try:
        from config import DB
        import psycopg2
        conn = psycopg2.connect(**DB)
        conn.close()
        print("DB=online")
    except Exception as exc:
        print(f"DB=OFFLINE: {exc}")


def check_wallet():
    try:
        from dialer.dialer import bolna_balance
        bal = bolna_balance(timeout=8)
        print(f"WALLET={bal.get('wallet')}|OK={bal.get('ok')}|ERR={bal.get('error')}")
    except Exception as exc:
        print(f"WALLET=None|OK=False|ERR={exc}")


if __name__ == "__main__":
    check_db()
    check_wallet()
