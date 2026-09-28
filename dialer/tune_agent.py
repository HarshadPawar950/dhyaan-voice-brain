"""
DHYAAN VOICE BRAIN — AGENT TUNER
================================
Adjusts a Bolna agent's TURN-TAKING knobs from the command line so you never
hunt field names in the dashboard again. SAFE BY DEFAULT: a bare run only READS;
a `set` is a DRY RUN (prints the before->after diff) unless you pass --go.

WHY THIS EXISTS
  The agents interrupt the caller. That is NOT controlled by the system prompt —
  it is decided by two Bolna config numbers:
    endpointing        how long a SILENCE the agent waits before deciding you are
                       done speaking. Raise it -> the agent stops cutting you off.
                       This is the #1 knob for "agent interrupts me".
    incremental_delay  a linear buffer added before the agent speaks. A gentle #2.
  (number_of_words_for_interruption is the OPPOSITE axis — how easily YOU can barge
   in on the agent — so we leave it alone; it does not fix agent-interrupts-caller.)

THE SHAPE TRAP (why this must be a script, not a raw curl)
  GET /agent/{id}  returns tasks at the TOP level:
       { id, agent_name, agent_type, tasks:[...], agent_prompts:{...}, ... }
  PUT /agent/{id}  is a FULL-OBJECT REPLACE and expects them WRAPPED:
       { agent_config:{ ...everything except server metadata... }, agent_prompts:{...} }
  Echo a GET straight into a PUT and you wipe the welcome message / webhook / voice.
  So we GET, back up the RAW json, reshape, mutate ONLY the two numbers, show the
  diff, and refuse the live PUT without --go.

  endpointing        -> tasks[<conversation>].tools_config.transcriber.endpointing (ms)
  incremental_delay  -> tasks[<conversation>].task_config.incremental_delay        (ms)

CLI
  python -m dialer.tune_agent show                                    # read BOTH agents' knobs
  python -m dialer.tune_agent show --agent dhyan                      # read one
  python -m dialer.tune_agent set --agent dhyan --endpointing 800 --delay 500        # DRY RUN (diff only)
  python -m dialer.tune_agent set --agent dhyan --endpointing 800 --delay 500 --go   # LIVE PUT (writes Bolna)

Isolation: talks ONLY to the Bolna Agent API. Touches no database, no portal.
"""
import os
import sys
import json
import argparse
from datetime import datetime

# Load .env BEFORE importing config (config reads os.getenv at import time).
from dotenv import load_dotenv  # noqa: E402
load_dotenv()

import requests  # noqa: E402

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (  # noqa: E402
    BOLNA_API_KEY, BOLNA_AGENTS, BOLNA_BASE_URL,
)

HTTP_TIMEOUT = 25
# Server-side metadata that must NOT be sent back inside agent_config on a PUT.
# Everything else the GET returned (agent_name, agent_type, tasks, welcome msg,
# webhook, voice, ...) is preserved so the full-object replace is loss-less.
_META_KEYS = {"id", "agent_status", "created_at", "updated_at", "agent_prompts"}

# Where raw config backups land (one per GET, timestamped). Never overwritten.
_BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_backups")


def _auth_header():
    return {"Authorization": f"Bearer {BOLNA_API_KEY}"}


def _bolna_error(r):
    """Human-readable error out of a Bolna error response."""
    try:
        j = r.json()
        if isinstance(j, dict):
            return (j.get("message") or j.get("detail") or j.get("error")
                    or j.get("error_message") or str(j))[:300]
    except Exception:
        pass
    return (r.text or "")[:300]


def _resolve(name):
    """Named caller -> agent UUID. Hard-stop if unconfigured so we never poke the
    wrong agent (mirrors dialer._agent's fail-loud rule)."""
    uuid = BOLNA_AGENTS.get(name.lower())
    if not uuid:
        have = ", ".join(sorted(BOLNA_AGENTS)) or "none"
        raise SystemExit(
            f"[TUNER] agent '{name}' not configured — set BOLNA_AGENT_"
            f"{name.upper()} in .env (configured now: {have})")
    return uuid


def _require_key():
    if not BOLNA_API_KEY:
        raise SystemExit("[TUNER] FATAL — BOLNA_API_KEY not set in .env. "
                         "Cannot read or write Bolna agents.")


def _get_agent(uuid):
    """GET /v2/agent/{id} — full config. Returns the parsed dict or dies loud.
    V2 because Bolna deprecated the V1 write API; we read V2 too so the GET->PUT
    round-trip shape stays consistent."""
    r = requests.get(f"{BOLNA_BASE_URL}/v2/agent/{uuid}",
                     headers=_auth_header(), timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise SystemExit(f"[TUNER] GET /v2/agent/{uuid} failed "
                         f"HTTP {r.status_code}: {_bolna_error(r)}")
    return r.json() if r.content else {}


def _has_system_prompt(cfg):
    """True if the fetched config carries a real (non-empty) system prompt. A PUT
    is a FULL replace that resends agent_prompts, so if the GET returned them
    empty/redacted we would BLANK the agent's brain — refuse the write in that
    case. agent_prompts shape: {task_1: {system_prompt: "..."}, ...}."""
    prompts = cfg.get("agent_prompts") or {}
    for task_prompts in prompts.values():
        if isinstance(task_prompts, dict) and (task_prompts.get("system_prompt") or "").strip():
            return True
    return False


def _backup(name, cfg):
    """Save the RAW GET response to a timestamped json BEFORE any mutation, so a
    bad PUT is always recoverable. Returns the path."""
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(_BACKUP_DIR, f"{name.lower()}_agent.bak_{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    return path


def _conversation_task(cfg):
    """Locate the conversation task in a GET response. That is where BOTH knobs
    live. Returns the task dict, or None if there is no conversation task."""
    for task in cfg.get("tasks", []) or []:
        if task.get("task_type") == "conversation":
            return task
    # Fallback: some agents label the first task loosely — use the first task
    # that has a transcriber, but WARN so the caller notices.
    for task in cfg.get("tasks", []) or []:
        if (task.get("tools_config") or {}).get("transcriber"):
            return task
    return None


def _read_knobs(task):
    """Current (endpointing, incremental_delay) from a conversation task. Either
    may be None if Bolna hasn't set it explicitly (then Bolna's own default is
    in force — 250ms endpointing / 400ms delay per the docs)."""
    tc = (task.get("tools_config") or {}).get("transcriber") or {}
    endp = tc.get("endpointing")
    delay = (task.get("task_config") or {}).get("incremental_delay")
    return endp, delay


def _fmt(v):
    return f"{v} ms" if isinstance(v, (int, float)) else f"{v} (unset -> Bolna default)"


# --- commands ---------------------------------------------------------------
def cmd_show(names):
    """Read-only: print each agent's current turn-taking knobs + back up its raw
    config (so we capture the real shape for a safe PUT later)."""
    _require_key()
    for name in names:
        uuid = _resolve(name)
        print("=" * 60)
        print(f"  {name.upper()}   ({uuid})")
        print("=" * 60)
        cfg = _get_agent(uuid)
        path = _backup(name, cfg)
        task = _conversation_task(cfg)
        if not task:
            print("  [!] no conversation task found — cannot read knobs.")
            print(f"  raw config backed up -> {path}")
            continue
        if task.get("task_type") != "conversation":
            print("  [!] no task_type=='conversation'; using first task with a "
                  "transcriber. Verify this is the right one.")
        endp, delay = _read_knobs(task)
        print(f"  endpointing       : {_fmt(endp)}   (silence before agent replies)")
        print(f"  incremental_delay : {_fmt(delay)}   (buffer before agent speaks)")
        print(f"  raw config backed up -> {path}")
    print("=" * 60)


def cmd_set(name, endpointing, delay, go):
    """Mutate ONLY endpointing / incremental_delay and (with --go) PUT it back.
    Dry run by default: shows the exact before->after and the reshape, writes
    nothing to Bolna."""
    _require_key()
    if endpointing is None and delay is None:
        raise SystemExit("[TUNER] nothing to set — pass --endpointing and/or --delay.")

    uuid = _resolve(name)
    cfg = _get_agent(uuid)
    backup_path = _backup(name, cfg)

    task = _conversation_task(cfg)
    if not task:
        raise SystemExit(f"[TUNER] {name}: no conversation task in config — "
                         f"refusing to guess. Raw saved -> {backup_path}")

    old_endp, old_delay = _read_knobs(task)

    # Mutate in place (agent_config below references these same task dicts).
    if endpointing is not None:
        tools = task.setdefault("tools_config", {})
        transcriber = tools.setdefault("transcriber", {})
        if not transcriber:
            print("  [!] this task had no transcriber block — creating one with "
                  "just endpointing. Verify in Bolna after the PUT.")
        transcriber["endpointing"] = endpointing
    if delay is not None:
        task.setdefault("task_config", {})["incremental_delay"] = delay

    new_endp, new_delay = _read_knobs(task)

    # Reshape GET -> PUT body: wrap everything except server metadata under
    # agent_config; agent_prompts rides alongside as its own top-level key.
    agent_config = {k: v for k, v in cfg.items() if k not in _META_KEYS}
    body = {"agent_config": agent_config, "agent_prompts": cfg.get("agent_prompts", {})}

    print("=" * 60)
    print(f"  TUNE {name.upper()}   ({uuid})" + ("   [LIVE]" if go else "   [DRY RUN]"))
    print("=" * 60)
    print(f"  endpointing       : {_fmt(old_endp)}  ->  {_fmt(new_endp)}")
    print(f"  incremental_delay : {_fmt(old_delay)}  ->  {_fmt(new_delay)}")
    print(f"  raw backup        : {backup_path}")
    print(f"  PUT body keys     : {sorted(body.keys())} "
          f"(agent_config has {len(agent_config)} keys)")
    prompt_ok = _has_system_prompt(cfg)
    print(f"  system prompt     : {'present (preserved)' if prompt_ok else 'MISSING in fetch (!)'}")
    print("=" * 60)

    if not go:
        print("[TUNER] DRY RUN -- nothing sent to Bolna. Re-run with --go to apply.")
        return

    if not prompt_ok:
        raise SystemExit(
            "[TUNER] ABORT — the fetched config has no system prompt, and a PUT "
            "would resend agent_prompts and BLANK the agent's brain. Refusing to "
            f"write. Your agent is UNCHANGED. Raw backup: {backup_path}")

    r = requests.put(f"{BOLNA_BASE_URL}/v2/agent/{uuid}", headers={
        **_auth_header(), "Content-Type": "application/json",
    }, json=body, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise SystemExit(f"[TUNER] PUT failed HTTP {r.status_code}: "
                         f"{_bolna_error(r)}\n  Your agent is UNCHANGED. Raw backup "
                         f"is safe at {backup_path}.")
    print(f"[TUNER] Bolna responded {r.status_code}. {name} updated. "
          f"Make a test call and listen.")


def cmd_prompt(name, path, go, welcome=None):
    """Replace the agent's system prompt (agent_prompts.task_1.system_prompt) from
    a local file, via the same V2 PUT. Dry run by default. Optionally also set the
    agent_welcome_message (--welcome) in the SAME atomic write — the welcome is the
    caller's cue to speak, so a bare-statement welcome causes dead air after the
    greeting. Turn-taking knobs are untouched."""
    _require_key()
    if not os.path.isfile(path):
        raise SystemExit(f"[TUNER] prompt file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        new_prompt = fh.read()
    if not new_prompt.strip():
        raise SystemExit(f"[TUNER] refusing to push an EMPTY prompt from {path}.")

    uuid = _resolve(name)
    cfg = _get_agent(uuid)
    backup_path = _backup(name, cfg)

    prompts = cfg.get("agent_prompts") or {}
    # Target the task that already holds a system prompt (task_1 for our agents).
    target_key = None
    for k, v in prompts.items():
        if isinstance(v, dict) and "system_prompt" in v:
            target_key = k
            break
    if not target_key:
        raise SystemExit(f"[TUNER] {name}: no agent_prompts.*.system_prompt found — "
                         f"refusing to guess where the prompt lives. Raw: {backup_path}")

    old_prompt = prompts[target_key].get("system_prompt") or ""
    prompts[target_key]["system_prompt"] = new_prompt

    agent_config = {k: v for k, v in cfg.items() if k not in _META_KEYS}
    old_welcome = cfg.get("agent_welcome_message") or ""
    if welcome is not None:
        agent_config["agent_welcome_message"] = welcome
    body = {"agent_config": agent_config, "agent_prompts": prompts}

    print("=" * 60)
    print(f"  PUSH PROMPT -> {name.upper()}   ({uuid})"
          + ("   [LIVE]" if go else "   [DRY RUN]"))
    print("=" * 60)
    print(f"  file        : {path}")
    print(f"  target      : agent_prompts.{target_key}.system_prompt")
    print(f"  length      : {len(old_prompt)} chars  ->  {len(new_prompt)} chars")
    print(f"  new head    : {new_prompt[:70]!r}")
    if welcome is not None:
        print(f"  welcome old : {old_welcome!r}")
        print(f"  welcome new : {welcome!r}")
    print(f"  raw backup  : {backup_path}")
    print("=" * 60)

    if not go:
        print("[TUNER] DRY RUN -- nothing sent to Bolna. Re-run with --go to apply.")
        return

    r = requests.put(f"{BOLNA_BASE_URL}/v2/agent/{uuid}", headers={
        **_auth_header(), "Content-Type": "application/json",
    }, json=body, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise SystemExit(f"[TUNER] PUT failed HTTP {r.status_code}: "
                         f"{_bolna_error(r)}\n  Agent UNCHANGED. Backup: {backup_path}.")
    print(f"[TUNER] Bolna responded {r.status_code}. {name} prompt updated. "
          f"Old prompt saved in {backup_path}.")


def main():
    p = argparse.ArgumentParser(description="Tune a Bolna agent's turn-taking knobs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="read current knobs (+ back up raw config)")
    p_show.add_argument("--agent", default=None,
                        help="agent name (dhyan/priya). Omit to show ALL configured.")

    p_set = sub.add_parser("set", help="set knobs (dry run unless --go)")
    p_set.add_argument("--agent", required=True, help="agent name (dhyan/priya)")
    p_set.add_argument("--endpointing", type=int, default=None,
                       help="silence-before-reply in ms (e.g. 800). The interrupt fix.")
    p_set.add_argument("--delay", type=int, default=None,
                       help="incremental_delay in ms (e.g. 500). Pre-speak buffer.")
    p_set.add_argument("--go", action="store_true",
                       help="ACTUALLY PUT to Bolna. Without this it's a dry run.")

    p_prompt = sub.add_parser("prompt", help="replace an agent's system prompt from a file")
    p_prompt.add_argument("--agent", required=True, help="agent name (dhyan/priya)")
    p_prompt.add_argument("--file", required=True, help="path to the new system prompt .txt")
    p_prompt.add_argument("--welcome", default=None,
                          help="also set the agent_welcome_message (caller's cue to speak)")
    p_prompt.add_argument("--go", action="store_true",
                          help="ACTUALLY PUT to Bolna. Without this it's a dry run.")

    args = p.parse_args()
    if args.cmd == "show":
        names = [args.agent] if args.agent else sorted(BOLNA_AGENTS)
        if not names:
            raise SystemExit("[TUNER] no agents configured — set BOLNA_AGENT_DHYAN "
                             "/ BOLNA_AGENT_PRIYA in .env.")
        cmd_show(names)
    elif args.cmd == "set":
        cmd_set(args.agent, args.endpointing, args.delay, args.go)
    elif args.cmd == "prompt":
        cmd_prompt(args.agent, args.file, args.go, welcome=args.welcome)


if __name__ == "__main__":
    main()
