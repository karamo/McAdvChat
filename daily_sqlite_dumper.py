import os
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import zstandard as zstd
import asyncio

class DailySQLiteDumper:
    def __init__(self, config_path="/etc/mcadvchat/config.json"):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        self.store_dir = Path(cfg["STORE_FILE_NAME"]).parent
        self.store_dir.mkdir(parents=True, exist_ok=True)

        self.prune_hours = cfg.get("PRUNE_HOURS", 168)
        self.compressor = zstd.ZstdCompressor()
        self._lock = asyncio.Lock()

    def _get_current_db_path(self):
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        return self.store_dir / f"mcdump_{date_str}.sqlite"

    def _ensure_db_schema(self, path):
        con = sqlite3.connect(path)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                timestamp TEXT NOT NULL,
                raw BLOB NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp)")
        con.commit()
        con.close()

    async def append_message(self, message: dict, raw: str):
        timestamp = datetime.utcnow().isoformat()
        compressed_raw = self.compressor.compress(raw.encode("utf-8"))
        db_path = self._get_current_db_path()

        await asyncio.to_thread(self._ensure_db_schema, db_path)

        async with self._lock:
            await asyncio.to_thread(self._write_to_db, db_path, timestamp, compressed_raw)

    def _write_to_db(self, path, timestamp, compressed_raw):
        con = sqlite3.connect(path)
        cur = con.cursor()
        cur.execute(
            "INSERT INTO messages (timestamp, raw) VALUES (?, ?)",
            (timestamp, compressed_raw)
        )
        con.commit()
        con.close()

    async def get_latest_timestamp(self):
        db_path = self._get_current_db_path()
        if not db_path.exists():
            return None
        return await asyncio.to_thread(self._fetch_latest_timestamp, db_path)

    def _fetch_latest_timestamp(self, path):
        con = sqlite3.connect(path)
        cur = con.cursor()
        cur.execute("SELECT MAX(timestamp) FROM messages")
        row = cur.fetchone()
        con.close()
        return row[0] if row and row[0] else None

    async def prune_old_files(self):
        cutoff = datetime.utcnow() - timedelta(hours=self.prune_hours)
        for file in self.store_dir.glob("mcdump_*.sqlite"):
            try:
                date_str = file.stem.split("_")[1]
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    file.unlink()
            except Exception as e:
                print(f"Failed to parse/delete file {file}: {e}")
