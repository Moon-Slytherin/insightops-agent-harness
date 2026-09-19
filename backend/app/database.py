"""SQLite 初始化和只读查询。"""

import csv
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "insightops.db"
CSV_PATH = DATA_DIR / "feedback.csv"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH)
    try:
        connection.row_factory = sqlite3.Row
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                version TEXT NOT NULL,
                category TEXT NOT NULL,
                urgency TEXT NOT NULL,
                channel TEXT NOT NULL,
                content TEXT NOT NULL,
                is_duplicate INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        count = connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        if count == 0:
            with CSV_PATH.open(encoding="utf-8-sig", newline="") as source:
                rows = list(csv.DictReader(source))
            connection.executemany(
                """
                INSERT INTO feedback
                (id, created_at, version, category, urgency, channel, content, is_duplicate)
                VALUES (:id, :created_at, :version, :category, :urgency, :channel, :content, :is_duplicate)
                """,
                rows,
            )


def query(sql: str, parameters: tuple = ()) -> list[dict]:
    """执行内部定义的只读 SQL，并返回普通字典。"""

    if not sql.lstrip().upper().startswith("SELECT"):
        raise ValueError("V1 数据工具只允许 SELECT 查询")
    with connect() as connection:
        return [dict(row) for row in connection.execute(sql, parameters).fetchall()]
