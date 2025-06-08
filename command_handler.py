#!/usr/bin/env python3
import asyncio
import hashlib
import json
import sys
import time
import re
from collections import defaultdict, deque

VERSION="v0.41.0"

has_console = sys.stdout.isatty()

# Command registry with handler functions and metadata
COMMANDS = {
    'search': {
        'handler': 'handle_search',
        'args': ['call', 'days'],
        'format': '!search call:CALL days:N',
        'description': 'Search messages by user and timeframe'
    },
    's': {
        'handler': 'handle_search',
        'args': ['call', 'days'],
        'format': '!search call:CALL days:N',
        'description': 'Search messages by user and timeframe'
    },
    'stats': {
        'handler': 'handle_stats', 
        'args': ['hours'],
        'format': '!stats hours:N',
        'description': 'Show message statistics for last N hours'
    },
    'mheard': {
        'handler': 'handle_mheard',
        'args': ['limit'],
        'format': '!mheard limit:N',
        'description': 'Show recently heard stations'
    },
    'mh': {
        'handler': 'handle_mheard',
        'args': ['limit'],
        'format': '!mheard limit:N',
        'description': 'Show recently heard stations'
    },
    'pos': {
        'handler': 'handle_position',
        'args': ['call', 'days'],
        'format': '!pos call:CALL days:N',
        'description': 'Show position data for callsign'
    },
    'help': {
        'handler': 'handle_help',
        'args': [],
        'format': '!help',
        'description': 'Show available commands'
    }
}

# Response chunking constants
MAX_RESPONSE_LENGTH = 140  # Maximum characters per message chunk
MAX_CHUNKS = 3            # Maximum number of response chunks
MSG_DELAY = 12  


class CommandHandler:
    def __init__(self, message_router=None, storage_handler=None, my_callsign = "DK0XXX"):
        self.message_router = message_router
        self.storage_handler = storage_handler
        self.my_callsign = my_callsign  # Your callsign to filter commands

        # Primary deduplication (msg_id based)
        self.processed_msg_ids = {}  # {msg_id: timestamp}
        self.msg_id_timeout = 5 * 60  # 5 minutes
        
        # Secondary throttling (content hash based)
        self.command_throttle = {}  # {content_hash: timestamp}
        self.throttle_timeout = 10 * 60  # 10 minutes
        
        # Abuse protection
        self.failed_attempts = {}  # {src: [timestamp, timestamp, ...]}
        self.max_failed_attempts = 3
        self.failed_attempt_window = 10 * 60  # 10 minutes
        self.block_duration = 30 * 60  # 30 minutes
        self.blocked_users = {}  # {src: block_timestamp}
        self.block_notifications_sent = set()
        
        # Subscribe to message types that might contain commands
        if message_router:
            message_router.subscribe('mesh_message', self._message_handler)
            message_router.subscribe('ble_notification', self._message_handler)
            
        if has_console:
            print(f"CommandHandler: Initialized with {len(COMMANDS)} commands")
            print(f"🐛 CommandHandler: Listening for commands to '{self.my_callsign}'")


    async def _message_handler(self, routed_message):
        """Handle incoming messages and check for commands"""
        message_data = routed_message['data']
     
        src_type = message_data.get('src_type')

        # Filter for messages directed to us
        dst = message_data.get('dst')
        if dst != self.my_callsign:
            return
            
        msg_text = message_data.get('msg', '')
        msg_text = re.sub(r'\{\d{3}$', '', msg_text)

        src_raw = message_data.get('src', 'unknown')
        if src_raw == "unknown":
           return
        src = src_raw.split(',')[0] if ',' in src_raw else src_raw

        
        # Check if message contains a command
        if not msg_text.startswith('!'):
            return
            
        if has_console:
            print(f"📋 CommandHandler: Processing command '{msg_text}' from {src}")

        if self._is_user_blocked(src):
            if has_console:
                print(f"🔴 CommandHandler: User {src} is blocked due to abuse")
            if src not in self.block_notifications_sent:
                self.block_notifications_sent.add(src)
                await self.send_response("🚫 {src} temporarily in timeout due to repeated invalid commands", src, src_type)
                if has_console:
                    print(f"🔴 CommandHandler: Sent block notification to {src}")
            else:
                if has_console:
                    print(f"🔴 CommandHandler: User {src} blocked - notification already sent, ignoring silently")
            return

        msg_id = message_data.get('msg_id')
        if self._is_duplicate_msg_id(msg_id):
            if has_console:
                print(f"🔄 CommandHandler: Duplicate msg_id {msg_id}, ignoring silently")
            return

        content_hash = self._get_content_hash(src, msg_text)
        if self._is_throttled(content_hash):
            if has_console:
                print(f"⏳ CommandHandler: THROTTLED - {src} command '{msg_text}' (hash: {content_hash})")
            await self.send_response("⏳ Command throttled. Same command allowed once per 10min", src, src_type)
            return

            
        # Parse and execute command
        try:
            cmd_result = self.parse_command(msg_text)
            if cmd_result:
                cmd, kwargs = cmd_result
                response = await self.execute_command(cmd, kwargs, src)

                self._mark_msg_id_processed(msg_id)
                self._mark_content_processed(content_hash)

                await self.send_response(response, src, src_type)

            else:
                # Track failed attempt
                self._track_failed_attempt(src)
                
                # Still mark msg_id as processed to prevent retries
                self._mark_msg_id_processed(msg_id)

                # Also throttle malformed commands
                self._mark_content_processed(content_hash)

                await self.send_response("❌ Unknown command. Try !help", src, src_type)
                
        except Exception as e:
            print(f"CommandHandler ERROR: {e}")

            # Track failed attempt
            self._track_failed_attempt(src)
            
            # Mark as processed even on error
            self._mark_msg_id_processed(msg_id)

            self._mark_content_processed(content_hash)

            await self.send_response(f"❌ Command failed: {str(e)[:50]}", src, src_type)

    def _get_content_hash(self, src, msg_text):
        """Create hash from source + full command (including arguments)"""
        content = f"{src}:{msg_text}"
        return hashlib.md5(content.encode()).hexdigest()[:8]

    def _is_duplicate_msg_id(self, msg_id):
        """Check msg_id cache and cleanup expired entries"""
        current_time = time.time()
        self._cleanup_msg_id_cache(current_time)
        return msg_id in self.processed_msg_ids

    def _is_throttled(self, content_hash):
        """Check throttle cache and cleanup expired entries"""
        current_time = time.time()
        self._cleanup_throttle_cache(current_time)
        return content_hash in self.command_throttle

    def _is_user_blocked(self, src):
        """Check if user is blocked and cleanup expired blocks"""
        current_time = time.time()
        self._cleanup_blocked_users(current_time)
        return src in self.blocked_users

    def _mark_msg_id_processed(self, msg_id):
        """Mark msg_id as processed"""
        self.processed_msg_ids[msg_id] = time.time()

    def _mark_content_processed(self, content_hash):
        """Mark content hash as processed"""
        self.command_throttle[content_hash] = time.time()

    def _track_failed_attempt(self, src):
        """Track failed command attempt and block if necessary"""
        current_time = time.time()
        
        # Initialize or get existing attempts
        if src not in self.failed_attempts:
            self.failed_attempts[src] = []
            
        # Add current attempt
        self.failed_attempts[src].append(current_time)
        
        # Clean old attempts outside the window
        cutoff = current_time - self.failed_attempt_window
        self.failed_attempts[src] = [
            timestamp for timestamp in self.failed_attempts[src] 
            if timestamp > cutoff
        ]
        
        # Check if user should be blocked
        if len(self.failed_attempts[src]) >= self.max_failed_attempts:
            self.blocked_users[src] = current_time
            if has_console:
                print(f"🚫 CommandHandler: BLOCKED user {src} for {self.block_duration/60} minutes due to {len(self.failed_attempts[src])} failed attempts")

    def _cleanup_msg_id_cache(self, current_time):
        """Remove old entries from msg_id cache"""
        cutoff = current_time - self.msg_id_timeout
        expired = [mid for mid, timestamp in self.processed_msg_ids.items() 
                   if timestamp < cutoff]
        for mid in expired:
            del self.processed_msg_ids[mid]

    def _cleanup_throttle_cache(self, current_time):
        """Remove old entries from throttle cache"""
        cutoff = current_time - self.throttle_timeout
        expired = [chash for chash, timestamp in self.command_throttle.items() 
                   if timestamp < cutoff]
        for chash in expired:
            del self.command_throttle[chash]

    def _cleanup_blocked_users(self, current_time):
        """Remove old entries from blocked users"""
        cutoff = current_time - self.block_duration
        expired = [src for src, timestamp in self.blocked_users.items() 
                   if timestamp < cutoff]
        for src in expired:
            del self.blocked_users[src]
            self.block_notifications_sent.discard(src)

            if has_console:
                print(f"🔓 CommandHandler: UNBLOCKED user {src}")

    def parse_command(self, msg_text):
        """Parse command text into command and arguments"""
        if not msg_text.startswith('!'):
            return None
            
        parts = msg_text[1:].split()
        if not parts:
            return None
            
        cmd = parts[0].lower()
        
        if cmd not in COMMANDS:
            return None
            
        # Parse key:value pairs
        kwargs = {}
        for part in parts[1:]:
            if ':' in part:
                key, value = part.split(':', 1)
                kwargs[key.lower()] = value
            else:
                # Handle positional arguments for simple commands
                if cmd == 'search' and not kwargs:
                    kwargs['call'] = part

                elif cmd == 'pos' and not kwargs:
                    kwargs['call'] = part

                elif cmd == 'stats' and not kwargs:
                    try:
                        kwargs['hours'] = int(part)
                    except ValueError:
                        pass

                elif cmd == 'mheard' and not kwargs:
                    try:
                        kwargs['limit'] = int(part)
                    except ValueError:
                        pass
                        
        return cmd, kwargs

    async def execute_command(self, cmd, kwargs, requester):
        """Execute a command and return response"""
        if cmd not in COMMANDS:
            return "❌ Unknown command"
            
        handler_name = COMMANDS[cmd]['handler']
        handler = getattr(self, handler_name, None)
        
        if not handler:
            return f"❌ Handler {handler_name} not implemented"
            
        try:
            return await handler(kwargs, requester)
        except Exception as e:
            return f"❌ Command error: {str(e)[:50]}"

    async def handle_search(self, kwargs, requester):
        """Search messages by user and timeframe - show summary with counts, last seen, and destinations"""
        user = kwargs.get('call', '*')
        days = int(kwargs.get('days', 1))
    
        if not self.storage_handler:
            return "❌ Message storage not available"
        
        # Determine search pattern
        if user != '*' and '-' not in user:
            # Callsign without SID: search for "CALL-*"
            search_pattern = user.upper() + '-'
            search_type = "prefix"  # Match anything starting with "DK5EN-"
            display_call = user.upper() + '-*'
        elif user != '*':
            # Callsign with SID: exact match (current behavior)
            search_pattern = user.upper()
            search_type = "exact"
            display_call = user.upper()
        else:
            # Wildcard: all users
            search_pattern = '*'
            search_type = "all"
            display_call = '*'
    
        # Search through message store
        cutoff_time = time.time() - (days * 24 * 60 * 60)
    
        msg_count = 0
        pos_count = 0
        last_msg_time = None
        last_pos_time = None
        destinations = set()  # Track unique destinations
        sids_activity = {}
    
        for item in reversed(list(self.storage_handler.message_store)):
            try:
                raw_data = json.loads(item["raw"])
                timestamp = raw_data.get('timestamp', 0)
            
                # Skip old messages
                if timestamp < cutoff_time * 1000:
                    continue
                
                src = raw_data.get('src', '')
                msg_type = raw_data.get('type', '')
                dst = raw_data.get('dst', '')
            
                # Apply search filter based on pattern type
                matched_callsigns = []
                if search_type == "all":
                    # Include all messages
                    matched_callsigns = [src.split(',')[0]]
                elif search_type == "prefix":
                    # Check if any callsign in src starts with the pattern
                    src_calls = [call.strip().upper() for call in src.split(',')]
                    matched_callsigns = [call for call in src_calls if call.startswith(search_pattern)]
                    if not matched_callsigns:
                        continue
                
                elif search_type == "exact":
                    # Check if exact callsign is in src
                    if search_pattern not in src.upper():
                        continue
                    matched_callsigns = [search_pattern]
                if search_type == "prefix":
                    for callsign in matched_callsigns:
                        if '-' in callsign:
                            sid = callsign.split('-')[1]
                            if sid not in sids_activity or timestamp > sids_activity[sid]:
                                sids_activity[sid] = timestamp
                
                # Count messages and track last seen times
                if msg_type == 'msg':
                    msg_count += 1
                    if last_msg_time is None or timestamp > last_msg_time:
                        last_msg_time = timestamp
                    
                    # Track numeric destinations only (public groups)
                    if dst and dst.isdigit():
                        destinations.add(dst)
                    
                elif msg_type == 'pos':
                    pos_count += 1
                    if last_pos_time is None or timestamp > last_pos_time:
                        last_pos_time = timestamp
                
            except (json.JSONDecodeError, KeyError):
                continue
            
        # Build response
        if msg_count == 0 and pos_count == 0:
            return f"🔍 No activity for {display_call} in last {days} day(s)"
        
        response = f"🔍 {display_call} ({days}d): "
    
        # Add message count and last seen
        if msg_count > 0:
            last_msg_str = time.strftime('%H:%M', time.localtime(last_msg_time/1000))
            response += f"{msg_count} msg (last {last_msg_str})"
        
        # Add separator if both types present
        if msg_count > 0 and pos_count > 0:
            response += " / "
        
        # Add position count and last seen
        if pos_count > 0:
            last_pos_str = time.strftime('%H:%M', time.localtime(last_pos_time/1000))
            response += f"{pos_count} pos (last {last_pos_str})"

        if search_type == "prefix" and sids_activity:
            # Sort SIDs by last activity (most recent first)
            sorted_sids = sorted(sids_activity.items(), key=lambda x: x[1], reverse=True)
            sid_info = []
            for sid, timestamp in sorted_sids:
                last_time = time.strftime('%H:%M', time.localtime(timestamp/1000))
                sid_info.append(f"-{sid} @{last_time}")
            response += f" / SIDs: {', '.join(sid_info)}"
        
        # Add destinations (numeric groups only)
        if destinations:
            sorted_destinations = sorted(destinations, key=int)  # Sort numerically
            response += f" / Groups: {','.join(sorted_destinations)}"
        
        return response

    async def handle_search_old(self, kwargs, requester):
       """Search messages by user and timeframe - show summary with counts and last seen"""
       user = kwargs.get('call', '*')
       days = int(kwargs.get('days', 1))
    
       if not self.storage_handler:
           return "❌ Message storage not available"
        
       # Search through message store
       cutoff_time = time.time() - (days * 24 * 60 * 60)
    
       msg_count = 0
       pos_count = 0
       last_msg_time = None
       last_pos_time = None
       destinations = set()
    
       for item in reversed(list(self.storage_handler.message_store)):
           try:
               raw_data = json.loads(item["raw"])
               timestamp = raw_data.get('timestamp', 0)
            
               # Skip old messages
               if timestamp < cutoff_time * 1000:
                   continue
                
               src = raw_data.get('src', '')
               msg_type = raw_data.get('type', '')
               dst = raw_data.get('dst', '')
            
               # Filter by user if specified
               if user != '*' and user.upper() not in src.upper():
                   continue
                
               # Count messages and track last seen times
               if msg_type == 'msg':
                   msg_count += 1
                   if last_msg_time is None or timestamp > last_msg_time:
                       last_msg_time = timestamp

                   if dst and dst.isdigit():
                      destinations.add(dst)
                    
               elif msg_type == 'pos':
                   pos_count += 1
                   if last_pos_time is None or timestamp > last_pos_time:
                       last_pos_time = timestamp
                
           except (json.JSONDecodeError, KeyError):
               continue
            
       # Build response
       if msg_count == 0 and pos_count == 0:
           return f"🔍 No activity for {user} in last {days} day(s)"
        
       response = f"🔍 {user} (in last {days}d): "
    
       # Add message count and last seen
       if msg_count > 0:
           last_msg_str = time.strftime('%H:%M', time.localtime(last_msg_time/1000))
           response += f"{msg_count} msg, last msg {last_msg_str}"
        
       # Add separator if both types present
       if msg_count > 0 and pos_count > 0:
           response += " / "
        
       # Add position count and last seen
       if pos_count > 0:
           last_pos_str = time.strftime('%H:%M', time.localtime(last_pos_time/1000))
           response += f"{pos_count} pos, last msg {last_pos_str}"

       if destinations:
           sorted_destinations = sorted(destinations, key=int)  # Sort numerically
           response += f" / Groups: {','.join(sorted_destinations)}"
        
       return response

    async def handle_stats(self, kwargs, requester):
        """Show message statistics"""
        hours = int(kwargs.get('hours', 24))
        
        if not self.storage_handler:
            return "❌ Message storage not available"
            
        cutoff_time = time.time() - (hours * 60 * 60)
        
        msg_count = 0
        pos_count = 0
        users = set()
        
        for item in self.storage_handler.message_store:
            try:
                raw_data = json.loads(item["raw"])
                timestamp = raw_data.get('timestamp', 0)
                
                if timestamp < cutoff_time * 1000:
                    continue
                    
                msg_type = raw_data.get('type', '')
                src = raw_data.get('src', '')
                
                if msg_type == 'msg':
                    msg_count += 1

                    if src:
                       users.add(src.split(',')[0])  # First callsign in path

                elif msg_type == 'pos':
                    pos_count += 1
                    
                    
            except (json.JSONDecodeError, KeyError):
                continue
                
        total = msg_count + pos_count
        avg_per_hour = round(total / max(hours, 1), 1)
        
        response = f"📊 Stats (last {hours}h): "
        response += f"Messages: {msg_count}, "
        response += f"Positions: {pos_count}, "
        response += f"Total: {total} ({avg_per_hour}/h), "
        response += f"Active stations: {len(users)}"
        
        return response

    async def handle_mheard(self, kwargs, requester):
        """Show recently heard stations"""
        limit = int(kwargs.get('limit', 5))
        
        if not self.storage_handler:
            return "❌ Message storage not available"
            
        # Get recent station activity
        stations = defaultdict(lambda: {'last_seen': 0, 'msg_count': 0})
        
        for item in list(self.storage_handler.message_store)[-4000:]:  # Last 4000 messages
            try:
                raw_data = json.loads(item["raw"])
                src = raw_data.get('src', '')
                timestamp = raw_data.get('timestamp', 0)
                msg_type = raw_data.get('type', '')

                if msg_type != 'msg':
                   continue
                
                if not src:
                    continue
                    
                # Use first callsign in path
                call = src.split(',')[0]
                if timestamp > stations[call]['last_seen']:
                    stations[call]['last_seen'] = timestamp
                stations[call]['msg_count'] += 1
                
            except (json.JSONDecodeError, KeyError):
                continue
                
        # Sort by last seen time
        sorted_stations = sorted(
            stations.items(), 
            key=lambda x: x[1]['last_seen'], 
            reverse=True
        )[:limit]
        
        if not sorted_stations:
            return "📻 No stations heard recently"
            
        response = "📻 MH: "
        station_info = []

        for call, data in sorted_stations:
            last_time = time.strftime('%H:%M', time.localtime(data['last_seen']/1000))
            #response += f"{call} @{last_time} ({data['msg_count']})\n"
            station_info.append(f"{call} @{last_time} ({data['msg_count']})")
            
        response += ", ".join(station_info)
        return response.rstrip()

    async def handle_position(self, kwargs, requester):
        """Show position data for callsign"""
        call = kwargs.get('call', '').upper()
        
        if not call:
            return "❌ Callsign required. Use: !pos call:CALL"
            
        if not self.storage_handler:
            return "❌ Message storage not available"
            
        for item in reversed(list(self.storage_handler.message_store)):
            try:
                raw_data = json.loads(item["raw"])
                
                if raw_data.get('type') != 'pos':
                    continue
                    
                src = raw_data.get('src', '')
                if call not in src.upper():
                    continue
                    
                lat = raw_data.get('lat')
                lon = raw_data.get('long')
                alt_ft = raw_data.get('alt')
                rssi = raw_data.get('rssi')
                snr = raw_data.get('snr')
                firmware = raw_data.get('firmware', '')
                lora_mod = raw_data.get('lora_mod')
                hw_id = raw_data.get('hw_id')
                
                if lat is not None and lon is not None:
                    lat_str = f"{lat:.2f}"
                    lon_str = f"{lon:.2f}"
                    mhloc = self._decode_maidenhead(lat, lon)

                    response = f"📍 {call}: {lat_str}, {lon_str}, {mhloc}"

                    if alt_ft:
                       alt_m = int(alt_ft * 0.3048)  # 1 ft = 0.3048 m
                       response += f" / {alt_m}m"
                
                    # Add RSSI if available
                    if rssi is not None:
                        response += f" / RSSI {rssi}"
                
                    # Add SNR if available  
                    if snr is not None:
                        response += f" / SNR {snr}"
                
                    # Add firmware if available
                    if firmware:
                        response += f" / FW: {firmware}"
                
                    # Add modulation info
                    if lora_mod is not None:
                        mod_text = self._decode_lora_modulation(lora_mod)
                        response += f" / Mod: {mod_text}"
                
                    # Add hardware info
                    if hw_id is not None:
                        hw_text = self._decode_hardware_id(hw_id)
                        response += f" / {hw_text}"

                    return response

            except (json.JSONDecodeError, KeyError):
                continue
                
        return f"📍 No position data for {call}"

    def _decode_lora_modulation(self, lora_mod):
      """Decode LoRa modulation value to readable format"""
      mod_map = {
        136: "EU8",
        # Add other mappings as needed
        # 137: "EU9", etc.
      }
      return mod_map.get(lora_mod, f"Mod{lora_mod}")

    def _decode_hardware_id(self, hw_id):
      """Decode hardware ID to readable format"""
      hw_map = {
        1: "TLoRa_V2",
        2: "TLoRa_V1", 
        3: "TLora_V2_1_1p6",
        4: "TBeam",
        5: "TBeam_1268",
        6: "TBeam_0p7",
        7: "T_Echo",
        8: "T_Deck",
        9: "RAK_4631",
        10: "Heltec_V2_1",
        11: "Heltec_V1",
        12: "T-Beam_APX2101",
        39: "E22",
        43: "Heltec_V3",
        44: "Heltec_E290",
        45: "TBeam_1262",
        46: "T_Deck_Plus",
        47: "T-Beam_Supreme",
        48: "ESP32_S3_EByte_E22",
      }
      return hw_map.get(hw_id, f"HW{hw_id}")

    def _decode_maidenhead(self, lat, lon):
          lon180=lon+180
          lat90=lat+90

          A=int((lon180)/20)
          B=int((lat90)/10)

          C=int(((lon180)%20)/2)
          D=int((lat90)%10)

          E=int(((lon180)%2)*12)
          F=int(((lat90)%1)*24)

          locator=f"{chr(A + ord('A'))}{chr(B + ord('A'))}{C}{D}{chr(E + ord('a'))}{chr(F + ord('a'))}"

          return locator

    async def handle_help(self, kwargs, requester):
        """Show available commands"""
        response = "📋 Available commands: "
        for cmd, info in COMMANDS.items():
            response += f"{info['format']}, "
            
        response += "Examples: "
        response += "!search user:DX0XX days:7, "
        response += "!stats 24, "
        response += "!mheard 5"
        
        return response

    async def send_response(self, response, recipient, src_type='udp'):
        """Send response back to requester, chunking if necessary"""
        if not response:
            return

        # Split response into chunks if too long
        chunks = self._chunk_response(response)
        
        for i, chunk in enumerate(chunks[:MAX_CHUNKS]):
            if len(chunks) > 1:
                chunk_header = f"({i+1}/{min(len(chunks), MAX_CHUNKS)}) "
                chunk = chunk_header + chunk

            if recipient.upper() == self.my_callsign:
                if has_console:
                    print(f"🔄 CommandHandler: Self-response, sending directly to WebSocket")

                # Send directly via WebSocket, bypass BLE routing
                if self.message_router:
                    websocket_message = {
                        'src': self.my_callsign,
                        'dst': recipient, 
                        'msg': chunk,
                        'src_type': 'ble',
                        'type': 'msg',
                        'timestamp': int(time.time() * 1000)
                    }
                    await self.message_router.publish('command', 'websocket_message', websocket_message)

            else:
              # Send via message router
              if self.message_router:
                  message_data = {
                      'dst': recipient,
                      'msg': chunk,
                      'src_type': 'command_response',
                      'type': 'msg'
                  }
              
                  # Route to appropriate protocol (BLE or UDP)
                  try:
                        if src_type=="ble":
                            await self.message_router.publish('command', 'ble_message', message_data)
                            if has_console:
                                print(f"📋 CommandHandler: Sent chunk {i+1} via BLE to {recipient}")
                        elif src_type=="udp" or src_type=="node":
                                # Update message data for UDP transport
                                message_data['src_type'] = 'command_response_udp'
                                await self.message_router.publish('command', 'udp_message', message_data)
                                if has_console:
                                    print(f"📋 CommandHandler: Sent chunk {i+1} via UDP to {recipient}")
                        else:
                            raise TransportUnavailableError("BLE and UDP not available")
                  except Exception as ble_error:
                        if has_console:
                            print(f"⚠️  CommandHandler: send failed to {recipient}: {ble_error}")
                            continue

                
            # Small delay between chunks
            if i < len(chunks) - 1:
                    await asyncio.sleep(12)
                    
            if has_console:
                print(f"📋 CommandHandler: Sent response chunk {i+1} to {recipient}")

    def _chunk_response(self, response):
        """Split response into chunks that fit message size limits"""
        if len(response) <= MAX_RESPONSE_LENGTH:
            return [response]
            
        chunks = []
        lines = response.split(', ')
        current_chunk = ""
        
        for line in lines:
            # If adding this line would exceed limit, start new chunk
            if len(current_chunk) + len(line) + 1 > MAX_RESPONSE_LENGTH:
                if current_chunk:
                    chunks.append(current_chunk.rstrip())
                    current_chunk = line
                else:
                    # Single line too long, truncate it
                    chunks.append(line[:MAX_RESPONSE_LENGTH-3] + "...")
            else:
                if current_chunk:
                    current_chunk += "\n" + line
                else:
                    current_chunk = line
                    
        if current_chunk:
            chunks.append(current_chunk.rstrip())
            
        return chunks


# Integration function for your main script
def create_command_handler(message_router, storage_handler, call_sign):
    """Factory function to create and integrate CommandHandler"""
    return CommandHandler(message_router, storage_handler, call_sign)
