from __future__ import annotations

import argparse
import json
import os

from grievance_api.core.config import load_config
from grievance_api.services.tracker_narrative_repair import repair_tracker_narratives


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair tracker summaries overwritten by low-priority action text.")
    parser.add_argument(
        "--config-path",
        default=os.getenv("APP_CONFIG_PATH", "/app/config/config.yaml"),
        help="Path to application config file",
    )
    parser.add_argument(
        "--db-path",
        default="",
        help="Optional sqlite database path override. Defaults to db_path from config.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates. Without this flag the command only prints matching and unresolved cases.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = str(args.db_path or "").strip()
    if not db_path:
        cfg = load_config(args.config_path)
        db_path = cfg.db_path

    result = repair_tracker_narratives(
        db_path=db_path,
        dry_run=not bool(args.apply),
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
