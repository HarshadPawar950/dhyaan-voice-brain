"""
DHYAAN VOICE BRAIN — replay raw webhooks through the parser (READ-ONLY).

Re-parses the last N lines of logs/raw_webhooks.jsonl with the CURRENT
catcher.parse_bolna and prints the 7 structured fields + verified/heat, so you can
confirm the "lead data" category is read correctly WITHOUT placing a call or
touching the DB. Raw is the source of truth (project rule #4) — this just reads it.

Run:  python -m tools.replay_webhook            # last 1 webhook
      python -m tools.replay_webhook --last 5   # last 5
"""
import os
import sys
import json
import argparse

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from catcher.server import parse_bolna           # noqa: E402

RAW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "logs", "raw_webhooks.jsonl")
_FIELDS = ("branch", "location", "budget", "configuration", "possession",
           "interested", "call_outcome")


def main():
    ap = argparse.ArgumentParser(prog="python -m tools.replay_webhook")
    ap.add_argument("--last", type=int, default=1, help="how many recent webhooks")
    args = ap.parse_args()

    if not os.path.exists(RAW):
        print(f"No raw webhook log yet at {RAW} — make a test call first.")
        return
    with open(RAW, "rb") as f:
        lines = [ln for ln in f.read().split(b"\n") if ln.strip()]
    if not lines:
        print("raw_webhooks.jsonl is empty — make a test call first.")
        return

    picked = lines[-args.last:]
    print("=" * 68)
    print(f"  REPLAY — last {len(picked)} webhook(s)   (READ-ONLY, no DB write)")
    print("=" * 68)
    for i, ln in enumerate(picked, 1):
        try:
            payload = json.loads(ln)
        except Exception as e:
            print(f"  [{i}] not valid JSON: {e}")
            continue
        # Did Bolna send the named category? (eyeball the raw shape.)
        ed = payload.get("extracted_data") or {}
        cats = list(ed.keys()) if isinstance(ed, dict) else "(not a dict)"
        row = parse_bolna(payload)
        print(f"  [{i}] exec={row.get('bolna_execution_id')}  dur={row.get('duration_sec')}s")
        print(f"      extracted_data categories: {cats}")
        for k in _FIELDS:
            print(f"      {k:<14}: {row.get(k)!r}")
        print(f"      -> verified={row.get('verified')}  heat={row.get('heat_score')}  "
              f"quality={row.get('call_quality')}  connection={row.get('connection_status')}")
        print("-" * 68)


if __name__ == "__main__":
    main()
