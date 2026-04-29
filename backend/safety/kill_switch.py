"""Emergency kill switch — halts all NEW live order placement.

Plain English:
  When `data/HALTED.flag` exists on disk, the bot will refuse to place any
  NEW order through `place_order_safe()`. Existing OPEN positions are NOT
  squared off automatically — normal exit logic (SL / Target / 15:15) keeps
  running so positions don't get abandoned. To force-close a stuck position,
  Ganesh closes it from the Kotak app.

How it gets armed:
  * From the dashboard header: click the red "STOP TRADING" button (only
    visible when LIVE_MODE is True). Confirm on the next page.
  * From SSH:        touch  data/HALTED.flag
  * To re-arm trading after a halt: rm  data/HALTED.flag   (deliberate
    ceremony — no web button to UN-halt; Ganesh must SSH in. This prevents
    a fat-finger from accidentally re-enabling live orders during an
    incident before he's investigated.)

Phase 3 — per-engine halt:
  In addition to the global flag, each logic engine (current/reverse) has
  its own flag file: `data/HALTED_current.flag`, `data/HALTED_reverse.flag`.
  A per-engine halt blocks NEW entries from that engine only; the other
  engine keeps trading. This is what auto-drawdown engages when one engine
  blows through its per-engine threshold while the other is still healthy.
  Manual ceremony to clear: rm data/HALTED_<engine>.flag (same as global).
"""
import os
from datetime import datetime

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HALT_FLAG_FILE = os.path.join(_REPO_ROOT, "data", "HALTED.flag")


def _engine_flag_path(engine):
    """Resolve the per-engine halt flag path. `engine` must be a known
    logic engine name; unknown names get their own flag file rather
    than crashing — defensive."""
    return os.path.join(_REPO_ROOT, "data", f"HALTED_{engine}.flag")


def is_halted():
    """True if the GLOBAL kill switch is currently engaged."""
    return os.path.exists(HALT_FLAG_FILE)


def is_engine_halted(engine):
    """True if the per-engine halt flag for `engine` is set. Does NOT
    check the global flag — callers compose the two checks themselves
    so paper-book ticks can gate on per-engine alone (preserving the
    'global kill switch does not freeze paper' invariant) while real
    ticks gate on `is_halted() or is_engine_halted(engine)`."""
    return os.path.exists(_engine_flag_path(engine))


def halt(reason="manual"):
    """Engage the global kill switch. Writes a small file with the reason +
    timestamp so an operator can later read why/when trading was halted."""
    os.makedirs(os.path.dirname(HALT_FLAG_FILE), exist_ok=True)
    with open(HALT_FLAG_FILE, "w") as f:
        f.write(f"halted_at={datetime.now().isoformat()}\nreason={reason}\n")


def halt_engine(engine, reason="manual"):
    """Engage the per-engine kill switch for `engine`. Writes
    `data/HALTED_<engine>.flag` with reason + timestamp. Does NOT touch
    the global flag, so the other logic engine keeps trading."""
    flag = _engine_flag_path(engine)
    os.makedirs(os.path.dirname(flag), exist_ok=True)
    with open(flag, "w") as f:
        f.write(f"halted_at={datetime.now().isoformat()}\n"
                f"engine={engine}\nreason={reason}\n")


def halt_info():
    """Return the contents of the GLOBAL halt flag (or None if not halted).
    Used by the dashboard header to show *when* and *why* trading was halted."""
    if not is_halted():
        return None
    try:
        with open(HALT_FLAG_FILE, "r") as f:
            return f.read()
    except Exception:
        return "halted (no detail available)"


def engine_halt_info(engine):
    """Return the contents of the per-engine halt flag (or None if not
    engaged). Lets the dashboard show 'reverse engine halted: drawdown
    Rs.X' separately from the global banner."""
    flag = _engine_flag_path(engine)
    if not os.path.exists(flag):
        return None
    try:
        with open(flag, "r") as f:
            return f.read()
    except Exception:
        return f"{engine} engine halted (no detail available)"
