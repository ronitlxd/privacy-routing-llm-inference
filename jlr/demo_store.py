"""Private database schema, optionally seeded with synthetic sample data."""
from __future__ import annotations
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contacts (alias TEXT PRIMARY KEY, label TEXT NOT NULL, email TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS places (alias TEXT PRIMARY KEY, label TEXT NOT NULL, address TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (sender TEXT NOT NULL, body TEXT NOT NULL, created INTEGER NOT NULL);
"""


class DemoStore:
    def __init__(self, path: str | Path | None = None, *, connection: sqlite3.Connection | None = None, seed: bool = True):
        self.owned = connection is None
        if connection is None:
            self.path = Path(path or Path(__file__).with_name("jlr_demo.sqlite3"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, check_same_thread=False)
        else:
            self.path = None
        self.db = connection
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if seed and not self.db.execute("SELECT 1 FROM profile LIMIT 1").fetchone():
            self.db.execute("INSERT INTO profile VALUES (?, ?)", ("driver", "Jordan Morgan"))
            self.db.executemany("INSERT INTO contacts VALUES (?, ?, ?)", [
                ("my wife", "Taylor Morgan", "taylor@example.invalid"),
                ("alex", "Alex Chen", "alex@example.invalid"),
            ])
            self.db.executemany("INSERT INTO places VALUES (?, ?, ?)", [
                ("home", "Home", "100 Demo Street, Toronto"),
                ("work", "Work", "200 Demo Avenue, Toronto"),
            ])
            self.db.executemany("INSERT INTO preferences VALUES (?, ?)", [
                ("usual_playlist", "Jazz"), ("usual_drink", "Oat latte"),
            ])
            self.db.execute("INSERT INTO messages VALUES (?, ?, ?)", ("Taylor Morgan", "I will be home after six.", int(time.time())))
        self.db.commit()

    def lookup(self, kind: str, value: str = ""):
        value = (value or "").casefold()
        if kind == "contact":
            row = self.db.execute("SELECT * FROM contacts WHERE alias=?", (value,)).fetchone()
            return dict(row) if row else None
        if kind == "place":
            row = self.db.execute("SELECT * FROM places WHERE alias=?", (value,)).fetchone()
            return dict(row) if row else None
        if kind == "preference":
            row = self.db.execute("SELECT value FROM preferences WHERE key=?", (value,)).fetchone()
            return row[0] if row else None
        if kind == "message":
            row = self.db.execute("SELECT sender, body FROM messages ORDER BY created DESC LIMIT 1").fetchone()
            return dict(row) if row else None
        return None

    def close(self):
        if self.owned:
            self.db.close()
