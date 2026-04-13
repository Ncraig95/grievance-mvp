from __future__ import annotations

import argparse
import json
import os

from grievance_api.core.config import load_config
from grievance_api.services.grievance_number_repair import repair_grievance_numbers


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Shift grievance numbers up by 1 from a cutoff case onward.")
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
        "--cutoff-id",
        default="2026026",
        help="Numeric grievance id to include as the first shifted case.",
    )
    parser.add_argument(
        "--expected-member-name",
        default="Dean Anderson",
        help="Fail unless the cutoff grievance id belongs to a matching member name. Pass empty string to disable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the renumbering plan without changing the database.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = str(args.db_path or "").strip()
    if not db_path:
        cfg = load_config(args.config_path)
        db_path = cfg.db_path

    result = repair_grievance_numbers(
        db_path=db_path,
        cutoff_id=args.cutoff_id,
        expected_member_name=(str(args.expected_member_name).strip() or None),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
