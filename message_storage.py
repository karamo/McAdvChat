#!/usr/bin/env python3
import asyncio
import concurrent.futures
import json
import os
import sys
import time
from collections import deque, defaultdict
from datetime import datetime, timedelta
from functools import partial
from statistics import mean
from collections import OrderedDict

VERSION="v0.46.0"

has_console = sys.stdout.isatty()

# Constants for message storage
BUCKET_SECONDS = 5 * 60
VALID_RSSI_RANGE = (-140, -30)
VALID_SNR_RANGE = (-30, 12)
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000

# Konstanten am Anfang der Klasse
GAP_THRESHOLD_MULTIPLIER = 6  # 30 minutes
MAX_DEBUG_SEGMENTS_SHOW = 10
MIN_DATAPOINTS_FOR_STATS = 100


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
    
    def __init__(self, message_store=None, max_size_mb=50, max_workers=None):
        self.message_store = message_store if message_store is not None else deque()
        self.message_store_size = 0
        self.max_size_mb = max_size_mb
        # Use 3 cores, leave 1 for main thread
        #self.max_workers = max_workers or min(4, os.cpu_count() - 1)
        self.max_workers = max_workers or max(2, os.cpu_count())
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
            if has_console:
               print(msg_content)
            return True
            
        if src_type == "BLE":
            return True
            
        if src == "response":
            return True

        if src_type == "TEST":
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
                    if ":ack" in raw:
                       continue
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

    def _process_message_chunk(self, messages_chunk, cutoff_timestamp_ms):
        """Process a chunk of messages in a worker thread"""
        chunk_buckets = defaultdict(lambda: {"rssi": [], "snr": []})
        
        for item in messages_chunk:
            raw_str = item.get("raw")
            if not raw_str:
                continue
                
            try:
                parsed = json.loads(raw_str)
            except json.JSONDecodeError:
                continue

            src = safe_get(parsed, "src")
            if not src:
                continue

            rssi = parsed.get("rssi")
            snr = parsed.get("snr") 
            timestamp_ms = parsed.get("timestamp")

            if timestamp_ms is None or timestamp_ms < cutoff_timestamp_ms:
                continue

            if not (is_valid_value(rssi, *VALID_RSSI_RANGE) and 
                   is_valid_value(snr, *VALID_SNR_RANGE)):
                continue

            bucket_time = floor_to_bucket(timestamp_ms)
            callsigns = [s.strip() for s in src.split(",")]

            for call in callsigns:
                key = (bucket_time, call)
                chunk_buckets[key]["rssi"].append(rssi)
                chunk_buckets[key]["snr"].append(snr)
                
        return chunk_buckets

    async def process_mheard_store_parallel_v2(self):
        """Parallelized version of process_mheard_store"""
        now_ms = int(time.time() * 1000)
        cutoff_timestamp_ms = now_ms - SEVEN_DAYS_MS

        # Split message store into chunks for parallel processing
        messages = list(self.message_store)
        if not messages:
            return []
            
        chunk_size = max(1, len(messages) // self.max_workers)
        chunks = [messages[i:i + chunk_size] for i in range(0, len(messages), chunk_size)]

        if has_console:
            print(f"📊 Processing {len(messages)} messages in {len(chunks)} chunks using {self.max_workers} workers")

        # Process chunks in parallel
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Create partial function with cutoff timestamp
            process_func = partial(self._process_message_chunk, cutoff_timestamp_ms=cutoff_timestamp_ms)
            
            # Submit all chunks for parallel processing
            future_to_chunk = {
                loop.run_in_executor(executor, process_func, chunk): i 
                for i, chunk in enumerate(chunks)
            }
            
            # Collect results as they complete
            all_buckets = defaultdict(lambda: {"rssi": [], "snr": []})
            completed = 0
            
            for future in asyncio.as_completed(future_to_chunk):
                chunk_buckets = await future
                completed += 1
                
                if has_console:
                    print(f"📊 Chunk {completed}/{len(chunks)} completed")
                
                # Merge chunk results into main buckets
                for key, values in chunk_buckets.items():
                    all_buckets[key]["rssi"].extend(values["rssi"])
                    all_buckets[key]["snr"].extend(values["snr"])

        # Calculate averages (this part is fast, keep sequential)
        result = []
        for (bucket_time, callsign), values in all_buckets.items():
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

        if has_console:
            print(f"📊 Parallel processing complete: {len(result)} statistics generated")
        
        return result

    def process_mheard_store(self):
        """Process message store for MHeard statistics (original sequential version)"""
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

    
    async def process_mheard_store_parallel(self):
        """Parallelized mheard processing with integrated gap detection and markers"""
        now_ms = int(time.time() * 1000)
        cutoff_timestamp_ms = now_ms - SEVEN_DAYS_MS
    
        # 1. Parallel chunk processing
        raw_buckets = await self._process_chunks_parallel(cutoff_timestamp_ms)
        if not raw_buckets:
            return []
    
        # 2. Convert buckets to time-ordered statistics
        raw_stats = self._buckets_to_stats(raw_buckets)
    
        # 3. Create segments with integrated gap markers
        final_result = self._create_segments_with_gaps(raw_stats)
    
        # 4. Consolidated logging
        self._log_processing_summary(raw_stats, final_result)
    
        return final_result
    
    async def _process_chunks_parallel(self, cutoff_timestamp_ms):
        """Handle parallel chunk processing with error handling"""
        messages = list(self.message_store)
        if not messages:
            return {}
    
        chunk_size = max(1, len(messages) // self.max_workers)
        chunks = [messages[i:i + chunk_size] for i in range(0, len(messages), chunk_size)]
    
        if has_console:
            print(f"📊 Processing {len(messages)} messages in {len(chunks)} chunks using {self.max_workers} workers")
    
        # Process chunks in parallel
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            process_func = partial(self._process_message_chunk, cutoff_timestamp_ms=cutoff_timestamp_ms)
            futures = [loop.run_in_executor(executor, process_func, chunk) for chunk in chunks]
            
            try:
                chunk_results = await asyncio.gather(*futures, return_exceptions=True)
                successful_chunks = sum(1 for r in chunk_results if not isinstance(r, Exception))
                
                if has_console:
                    print(f"📊 Successfully processed {successful_chunks}/{len(chunks)} chunks")
                    
                # Merge all buckets
                all_buckets = defaultdict(lambda: {"rssi": [], "snr": []})
                for result in chunk_results:
                    if isinstance(result, Exception):
                        continue
                    for key, values in result.items():
                        all_buckets[key]["rssi"].extend(values["rssi"])
                        all_buckets[key]["snr"].extend(values["snr"])
                        
                return all_buckets
                
            except Exception as e:
                if has_console:
                    print(f"📊 Parallel processing failed: {e}")
                return {}
    
    def _buckets_to_stats(self, all_buckets):
        """Convert bucket data to time-ordered statistics"""
        sorted_keys = sorted(all_buckets.keys(), key=lambda x: x[0])
        
        if has_console:
            print(f"📊 Generated {len(sorted_keys)} time-ordered buckets")
    
        stats = []
        for bucket_time, callsign in sorted_keys:
            values = all_buckets[(bucket_time, callsign)]
            rssi_values = values["rssi"]
            snr_values = values["snr"]
            count = min(len(rssi_values), len(snr_values))
    
            if count > 0:
                stats.append({
                    "src_type": "STATS",
                    "timestamp": bucket_time,
                    "callsign": callsign,
                    "rssi": round(mean(rssi_values), 2),
                    "snr": round(mean(snr_values), 2),
                    "count": count
                })
        
        return stats
    
    def _create_segments_with_gaps(self, raw_stats):
        """Create segments and insert gap markers in single pass"""
        gap_threshold = GAP_THRESHOLD_MULTIPLIER * BUCKET_SECONDS
        final_result = []
        filtered_callsigns = []
        
        # Process each callsign separately
        for callsign, entries in self._group_by_callsign(raw_stats).items():
            if len(entries) < MIN_DATAPOINTS_FOR_STATS:
                filtered_callsigns.append((callsign, len(entries)))
                continue
            callsign_result = self._process_callsign_timeline(callsign, entries, gap_threshold)
            final_result.extend(callsign_result)

        if has_console and filtered_callsigns:
            filtered_callsigns.sort(key=lambda x: x[1], reverse=True)  # Sort by data point count
            print(f"📊 Filtered {len(filtered_callsigns)} callsigns with <{MIN_DATAPOINTS_FOR_STATS} data points:")
            for callsign, count in filtered_callsigns[:MAX_DEBUG_SEGMENTS_SHOW]:
                print(f"📊   {callsign}: {count} points (filtered)")
        
        return sorted(final_result, key=lambda x: (x["callsign"], x["timestamp"]))
    
    def _group_by_callsign(self, stats):
        """Group statistics by callsign and sort by timestamp"""
        grouped = defaultdict(list)
        for entry in stats:
            grouped[entry["callsign"]].append(entry)
        
        # Sort each group by timestamp
        for callsign in grouped:
            grouped[callsign].sort(key=lambda x: x["timestamp"])
        
        return grouped
    
    def _process_callsign_timeline(self, callsign, entries, gap_threshold):
        """Process single callsign: detect gaps and create markers in one pass"""
        if not entries:
            return []
        
        result = []
        segment_id = 0
        current_segment = []
        
        for i, entry in enumerate(entries):
            # Check for gap (but not on first entry)
            if i > 0:
                time_gap = entry["timestamp"] - entries[i-1]["timestamp"]
                
                if time_gap > gap_threshold:
                    # Finalize current segment
                    if current_segment:
                        self._finalize_segment(current_segment, callsign, segment_id)
                        result.extend(current_segment)
                    
                    # Insert gap marker
                    gap_marker = self._create_gap_marker(
                        callsign, entry["timestamp"], segment_id, segment_id + 1
                    )
                    result.append(gap_marker)
                    
                    # Start new segment
                    segment_id += 1
                    current_segment = []
            
            # Add entry to current segment
            entry["segment_id"] = f"{callsign}_seg_{segment_id}"
            current_segment.append(entry)
        
        # Finalize last segment
        if current_segment:
            self._finalize_segment(current_segment, callsign, segment_id)
            result.extend(current_segment)
        
        return result
    
    def _finalize_segment(self, segment, callsign, segment_id):
        """Add metadata to all entries in a segment"""
        segment_size = len(segment)
        for entry in segment:
            entry["segment_size"] = segment_size
    
    def _create_gap_marker(self, callsign, next_timestamp, from_seg, to_seg):
        """Create a gap marker for Chart.js"""
        return {
            "src_type": "STATS", 
            "timestamp": next_timestamp - BUCKET_SECONDS,
            "callsign": callsign,
            "rssi": None,
            "snr": None,
            "count": None,
            "segment_id": f"{callsign}_gap_{from_seg}_to_{to_seg}",
            "segment_size": 1,
            "is_gap_marker": True
        }
    
    def _log_processing_summary(self, raw_stats, final_result):
        """Consolidated logging for processing summary"""
        if not has_console:
            return
            
        # Basic statistics
        gap_markers = [r for r in final_result if r.get("is_gap_marker")]
        stats_entries = [r for r in final_result if not r.get("is_gap_marker")]

        # **ENHANCED: Show filtering impact**
        total_callsigns_raw = len(set(entry["callsign"] for entry in raw_stats))
        final_callsigns = len(set(entry["callsign"] for entry in stats_entries))
        filtered_count = total_callsigns_raw - final_callsigns
    
        print(f"📊 Added {len(gap_markers)} gap markers for Chart.js compatibility")
        print(f"📊 Final result: {len(final_result)} total points ({len(stats_entries)} stats + {len(gap_markers)} gaps)")
        print(f"📊 Callsigns: {final_callsigns} included, {filtered_count} filtered (min {MIN_DATAPOINTS_FOR_STATS} points)")
    
        
        # Segment statistics by callsign
        segment_stats = defaultdict(int)
        gap_details = []
        
        for marker in gap_markers:
            callsign = marker["callsign"]
            segment_stats[callsign] += 1
            # Extract gap duration info could be added here if needed
        
        # Show top callsigns with most segments
        top_segmented = sorted(segment_stats.items(), key=lambda x: x[1], reverse=True)
        for callsign, gap_count in top_segmented[:MAX_DEBUG_SEGMENTS_SHOW]:
            total_segments = gap_count + 1  # gaps + 1 = segments
            if total_segments > 1:
                print(f"📊 {callsign}: {total_segments} segments ({gap_count} gaps)")
        
        # Time range summary
        if stats_entries:
            first_ts = min(entry["timestamp"] for entry in stats_entries)
            last_ts = max(entry["timestamp"] for entry in stats_entries)
            span_hours = (last_ts - first_ts) / 3600
            unique_callsigns = len(set(entry["callsign"] for entry in stats_entries))
            
            print(f"📊 Timestamp range: {first_ts} → {last_ts} (span: {span_hours:.1f}h)")
            print(f"📊 Processed {unique_callsigns} callsigns with {len(segment_stats)} having gaps")
    
