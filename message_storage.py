#!/usr/bin/env python3
import json
import time
from collections import deque, defaultdict
from datetime import datetime, timedelta
from statistics import mean

VERSION="v0.37.0"

# Constants for message storage
BUCKET_SECONDS = 5 * 60
VALID_RSSI_RANGE = (-140, -30)
VALID_SNR_RANGE = (-30, 12)
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000


def get_current_timestamp() -> str:
    """Get current UTC timestamp in ISO format"""
    return datetime.utcnow().isoformat()


def safe_get(raw_data, key, default=""):
    """
    Safely retrieves a key from raw_data, which might be:
    - a dict
    - a JSON-encoded string
    - a random string or malformed object
    Returns default if anything fails.
    """
    try:
        if isinstance(raw_data, str):
            try:
                raw_data = json.loads(raw_data)
            except json.JSONDecodeError:
                return default

        if isinstance(raw_data, dict):
            return raw_data.get(key, default)

    except Exception as e:
        # Optionally log e
        return default

    return default


def is_valid_value(value, min_val, max_val):
    """Check if value is within valid range"""
    return isinstance(value, (int, float)) and min_val <= value <= max_val


def floor_to_bucket(unix_ms):
    """Floor timestamp to bucket boundary"""
    return int(unix_ms // 1000 // BUCKET_SECONDS * BUCKET_SECONDS)


class MessageStorageHandler:
    """Handles message storage and retrieval operations"""
    
    def __init__(self, message_store=None, max_size_mb=50):
        self.message_store = message_store if message_store is not None else deque()
        self.message_store_size = 0
        self.max_size_mb = max_size_mb
        self._recalculate_size()
        
    def _recalculate_size(self):
        """Recalculate the current storage size"""
        self.message_store_size = sum(
            len(json.dumps(item).encode("utf-8")) 
            for item in self.message_store
        )
    
    async def store_message(self, message: dict, raw: str):
        """Store a message with automatic size management"""
        if not isinstance(message, dict):
            print("store_message: invalid input, message is None or not a dict")
            return

        timestamped = {
            "timestamp": get_current_timestamp(),
            "raw": raw
        }

        # Filter out unwanted messages
        if self._should_filter_message(message):
            return

        message_size = len(json.dumps(timestamped).encode("utf-8"))
        self.message_store.append(timestamped)
        self.message_store_size += message_size
        
        # Manage size limits
        while self.message_store_size > self.max_size_mb * 1024 * 1024:
            removed = self.message_store.popleft()
            self.message_store_size -= len(json.dumps(removed).encode("utf-8"))

    def _should_filter_message(self, message: dict) -> bool:
        """Check if message should be filtered out"""
        msg_content = message.get("msg", "<no msg>")
        src_type = message.get("src_type", "<no type>")
        src = message.get("src", "<no src>")
        
        # Filter conditions
        if msg_content.startswith("{CET}"):
            print(msg_content)
            return True
            
        if src_type == "BLE":
            return True
            
        if src == "response":
            return True
            
        if msg_content == "-- invalid character --":
            return True
            
        if "No core dump" in msg_content:
            return True
            
        return False
    
    def get_message_count(self) -> int:
        """Get current message count"""
        return len(self.message_store)
    
    def get_storage_size_mb(self) -> float:
        """Get current storage size in MB"""
        return self.message_store_size / (1024 * 1024)

    def prune_messages(self, prune_hours, block_list):
        """Prune old messages and blocked sources"""
        cutoff = datetime.utcnow() - timedelta(hours=prune_hours)
        temp_store = deque()
        new_size = 0

        for item in self.message_store:
            try:
                raw_data = json.loads(item["raw"])
            except (KeyError, json.JSONDecodeError) as e:
                print(f"Skipping item due to malformed 'raw': {e}")
                continue

            msg = safe_get(raw_data, "msg")
            if msg == "-- invalid character --":
                print(f"invalid character suppressed from {raw_data.get('src')}")
                continue

            if "No core dump" in msg:
                print(f"core dump messages suppressed: {raw_data.get('msg')} {raw_data.get('src')}")
                continue

            src = safe_get(raw_data, "src")
            if src in block_list:
                print(f"Blocked src: {raw_data.get('src')}")
                continue

            try:
                timestamp = datetime.fromisoformat(item["timestamp"])
            except ValueError as e:
                print(f"Skipping item due to bad timestamp: {e}")
                continue

            if timestamp > cutoff:
                temp_store.append(item)
                new_size += len(json.dumps(item).encode("utf-8"))

        self.message_store.clear()
        self.message_store.extend(temp_store)
        self.message_store_size = new_size
        print(f"After message cleaning {len(self.message_store)}")

    def load_dump(self, filename):
        """Load message store from file"""
        import os
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                self.message_store = deque(loaded)
                self._recalculate_size()
                print(f"{len(self.message_store)} Nachrichten ({self.message_store_size / 1024:.2f} KB) geladen")

    def save_dump(self, filename):
        """Save message store to file"""
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(list(self.message_store), f, ensure_ascii=False, indent=2)
        print(f"Stored {len(self.message_store)} messages to {filename}")

    def get_initial_payload(self):
        """Get initial payload for websocket clients"""
        recent_items = list(reversed(self.message_store))
        msgs_per_dst = defaultdict(list)
        pos_per_src = defaultdict(list)

        for i in recent_items:
            raw = i["raw"]

            if '"type": "msg"' in raw:
                try:
                    data = json.loads(raw)
                    dst = data.get("dst")
                    if (dst is not None and len(msgs_per_dst[dst]) < 50):
                        msgs_per_dst[dst].append(raw)
                except json.JSONDecodeError:
                    continue

            elif '"type": "pos"' in raw:
                try:
                    data = json.loads(raw)
                    src = data.get("src")
                    if (src is not None and len(pos_per_src[src]) < 50):
                        pos_per_src[src].append(raw)
                except json.JSONDecodeError:
                    continue

        # Flatten all dst buckets back into a single list
        msg_msgs = []
        for msg_list in msgs_per_dst.values():
            msg_msgs.extend(reversed(msg_list))

        pos_msgs = []
        for pos_list in pos_per_src.values():
            pos_msgs.extend(pos_list)

        return msg_msgs + pos_msgs

    def get_full_dump(self):
        """Get full message dump"""
        msg_items = [item for item in self.message_store
                     if json.loads(item["raw"]).get("type") == "msg"]
        return [item["raw"] for item in msg_items]

    def process_mheard_store(self):
        """Process message store for MHeard statistics"""
        now_ms = int(time.time() * 1000)
        cutoff_timestamp_ms = now_ms - SEVEN_DAYS_MS

        buckets = defaultdict(lambda: {"rssi": [], "snr": []})

        for item in self.message_store:
            raw_str = item.get("raw")
        
            if not raw_str:
                print("not str")
                continue
            try:
                parsed = json.loads(raw_str)
            except json.JSONDecodeError:
                continue

            src = safe_get(parsed, "src")
            
            if not src:
                continue

            callsigns = [s.strip() for s in src.split(",")]

            rssi = parsed.get("rssi")
            snr = parsed.get("snr")
            timestamp_ms = parsed.get("timestamp")

            if timestamp_ms is None or timestamp_ms < cutoff_timestamp_ms:
                continue

            if not (is_valid_value(rssi, *VALID_RSSI_RANGE) and is_valid_value(snr, *VALID_SNR_RANGE)):
                continue

            bucket_time = floor_to_bucket(timestamp_ms)

            for call in callsigns:
                key = (bucket_time, call)
                buckets[key]["rssi"].append(rssi)
                buckets[key]["snr"].append(snr)

        # Average and build output
        result = []
        for (bucket_time, callsign), values in buckets.items():
            rssi_values = values["rssi"]
            snr_values = values["snr"]
            count = min(len(rssi_values), len(snr_values))

            if count == 0:
                continue

            avg_rssi = round(mean(rssi_values), 2)
            avg_snr = round(mean(snr_values), 2)
            result.append({
                "src_type": "STATS",
                "timestamp": bucket_time,
                "callsign": callsign,
                "rssi": avg_rssi,
                "snr": avg_snr,
                "count": count
            })

        return result
