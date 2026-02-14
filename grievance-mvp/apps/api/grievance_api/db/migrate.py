from __future__ import annotations

import pathlib
import sqlite3

SCHEMA_PATH = pathlib.Path(__file__).with_name("schema.sql")

def migrate(db_path: str) -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    con = sqlite3.connect(db_path)
    try:
        con.executescript(schema)
        con.commit()
    finally:
        con.close()
