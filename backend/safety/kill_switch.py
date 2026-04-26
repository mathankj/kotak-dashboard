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
"""
import os
from datetime import datetime

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HALT_FLAG_FILE = os.path.join(_REPO_ROOT, "data", "HALTED.flag")


def is_halted():
    """True if the kill switch is currently engaged."""
    return os.path.exists(HALT_FLAG_FILE)


def halt(reason="manual"):
    """Engage the kill switch. Writes a small file with the reason + timestamp
    so an operator can later read why/when trading was halted."""
    os.makedirs(os.path.dirname(HALT_FLAG_FILE), exist_ok=True)
    with open(HALT_FLAG_FILE, "w") as f:
        f.write(f"halted_at={datetime.now().isoformat()}\nreason={reason}\n")


def halt_info():
    """Return the contents of the halt flag (or None if not halted).
    Used by the dashboard header to show *when* and *why* trading was halted."""
    if not is_halted():
        return None
    try:
        with open(HALT_FLAG_FILE, "r") as f:
            return f.read()
    except Exception:
        return "halted (no detail available)"
