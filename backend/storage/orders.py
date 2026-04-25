"""Orders log: every place_order attempt is appended here for audit."""
import json
import os

ORDERS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "orders_log.json",
)


def append_order(entry):
    """Append an order attempt to orders_log.json. Newest first, max 200."""
    try:
        existing = []
        if os.path.exists(ORDERS_FILE):
            with open(ORDERS_FILE, "r") as f:
                existing = json.load(f)
        existing.insert(0, entry)
        existing = existing[:200]
        with open(ORDERS_FILE, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass


def read_orders():
    try:
        if os.path.exists(ORDERS_FILE):
            with open(ORDERS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []
