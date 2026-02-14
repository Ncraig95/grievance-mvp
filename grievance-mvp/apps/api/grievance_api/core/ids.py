from __future__ import annotations

import secrets
from datetime import datetime, timezone

def new_grievance_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"G{ts}_{secrets.token_hex(4)}"
