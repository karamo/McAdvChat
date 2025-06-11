#!/usr/bin/env python3
import asyncio
import hashlib
import json
import sys
import time
import re
import random
from datetime import datetime
from collections import defaultdict, deque
from meteo import WeatherService

VERSION="v0.47.0"

# Response chunking constants
MAX_RESPONSE_LENGTH = 140  # Maximum characters per message chunk
MAX_CHUNKS = 3            # Maximum number of response chunks
MSG_DELAY = 12  

DEFAULT_THROTTLE_TIMEOUT = 5 * 60  # 5 minutes default

COMMAND_THROTTLING = {
    'dice': 5,      # 5 seconds for dice games
    'time': 5,      # 5 seconds for time requests
    'group': 5,
    # All other commands use default 5 minutes
}


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
        'format': '!mheard type:all|msg|pos limit:N',
        'description': 'Show recently heard stations'
    },
    'mh': {
        'handler': 'handle_mheard',
        'args': ['limit'],
        'format': '!mheard type:all|msg|pos limit:N',
        'description': 'Show recently heard stations'
    },
    'pos': {
        'handler': 'handle_position',
        'args': ['call', 'days'],
        'format': '!pos call:CALL days:N',
        'description': 'Show position data for callsign'
    },
    'dice': {
        'handler': 'handle_dice',
        'args': [],
        'format': '!dice',
        'description': 'Roll two dice with Mäxchen rules'
    },
    'time': {
        'handler': 'handle_time',
        'args': [],
        'format': '!time',
        'description': 'Show nodes time and date'
    },
    'wx': {
        'handler': 'handle_weather',
        'args': [],
        'format': '!wx',
        'description': 'Show nodes current weather'
    },
    'weather': {
        'handler': 'handle_weather', 
        'args': [],
        'format': '!weather',
        'description': 'Show nodes current weather'
    },
    'group': {
        'handler': 'handle_group_control',
        'args': ['state'],
        'format': '!group on|off',
        'description': 'Control group response mode (admin only)'
    },
    'help': {
        'handler': 'handle_help',
        'args': [],
        'format': '!help',
        'description': 'Show available commands'
    }
}


class CommandHandler:
    def __init__(self, message_router=None, storage_handler=None, my_callsign = "DK0XXX", lat = 48.4031, lon = 11.7497, stat_name = "Freising"):
        self.message_router = message_router
        self.storage_handler = storage_handler
        self.my_callsign = my_callsign  # Your callsign to filter commands
        self.admin_callsign_base = my_callsign.split('-')[0]
        self.lat = lat
        self.lon = lon
        self.stat_name = stat_name
        self.group_responses_enabled = False  # Default OFF

        try:
            self.weather_service = WeatherService(self.lat, self.lon, self.stat_name, max_age_minutes=30)
            if has_console:
                print(f"🌤️  CommandHandler: Weather service initialized for {self.lat}/{self.lon}")
        except ImportError as e:
            self.weather_service = None
            if has_console:
                print(f"❌ CommandHandler: Weather service unavailable: {e}")

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
            print(f"🐛 CommandHandler: Weather service initialized for {self.lat}/{self.lon}")


    def _is_admin(self, callsign):
        """Check if callsign is admin (DK5EN with any SID)"""
        if not callsign:
            return False
        base_call = callsign.split('-')[0] if '-' in callsign else callsign
        return base_call.upper() == self.admin_callsign_base

    async def handle_group_control(self, kwargs, requester):
        """Control group response mode (admin only)"""
        if has_console:
            print(f"🔍 handle_group_control called with kwargs={kwargs}, requester='{requester}'")
        
        if not self._is_admin(requester):
            if has_console:
                print(f"🔍 Admin check failed for '{requester}'")
            return "❌ Admin access required"
        
        state = kwargs.get('state', '').lower()
        if has_console:
            print(f"🔍 Extracted state: '{state}'")

        if state == 'on':
            self.group_responses_enabled = True
            if has_console:
                print(f"🔍 Set group_responses_enabled = True")
            return "✅ Group responses ENABLED"
        elif state == 'off':
            self.group_responses_enabled = False
            if has_console:
                print(f"🔍 Set group_responses_enabled = False")
            return "✅ Group responses DISABLED"
        else:
            current = "ON" if self.group_responses_enabled else "OFF"
            if has_console:
                print(f"🔍 No valid state, current setting: {current}")
            return f"🔧 Group responses: {current}. Use !group on|off"
    

    def _is_valid_target(self, dst, src):
        """Check if message is for us (callsign) or valid group (1-5 digits or 'TEST')"""
        # Always allow direct messages to our callsign
        if dst == self.my_callsign:
            return True, 'callsign'
        
        # Check if dst is a valid group format
        is_valid_group = dst == 'TEST' or (dst and dst.isdigit() and 1 <= len(dst) <= 5)
        if not is_valid_group:
            return False, None
        
        # Admin always allowed for groups
        if self._is_admin(src):
            return True, 'group'
        
        # Non-admin only allowed if group responses are enabled
        if self.group_responses_enabled:
            return True, 'group'
        
        return False, None

    async def handle_weather(self, kwargs, requester):
        try:
            if has_console:
                print(f"🌤️  CommandHandler: Getting weather data for {requester}")
            
            # Wetterdaten abrufen (kann etwas dauern)
            weather_data = self.weather_service.get_weather_data()
            
            if "error" in weather_data:
                if has_console:
                    print(f"❌ Weather error: {weather_data['error']}")
                return f"❌ Weather unavailable: {weather_data['error'][:30]}"
            
            # Ham Radio optimiertes LoRa-Format verwenden
            weather_msg = self.weather_service.format_for_lora(weather_data)
            
            # Zusätzliche Info für Logs
            if has_console:
                source = weather_data.get('data_source', 'Unknown')
                quality = weather_data.get('data_quality', 'Unknown')
                age = weather_data.get('data_age_minutes', 0)
                print(f"✅ Weather delivered: {source}, Quality: {quality}, Age: {age:.1f}min")
                
                # Debug: zeige auch supplemented parameters
                if 'supplemented_parameters' in weather_data and weather_data['supplemented_parameters']:
                    supplemented = ', '.join(weather_data['supplemented_parameters'])
                    print(f"🔗 Fusion used: {supplemented} from OpenMeteo")
            
            return weather_msg
            
        except Exception as e:
            error_msg = f"Weather service error: {str(e)[:40]}"
            if has_console:
                print(f"❌ Weather handler error: {e}")
            return f"❌ {error_msg}"


    async def _message_handler(self, routed_message):
        """Handle incoming messages and check for commands"""
        message_data = routed_message['data']

        src_type = message_data.get('src_type')

        if 'msg' not in message_data:
            return

        msg_text = message_data.get('msg', '')
        if not msg_text or not msg_text.startswith('!'):
            return

        # Filter for messages directed to us
        dst = message_data.get('dst')

        src_raw = message_data.get('src', 'unknown')
        if not src_raw or src_raw == "unknown":
            if has_console:
                print(f"🐛 CommandHandler: Invalid src: '{src_raw}', skipping")
            return

        src = src_raw.split(',')[0] if ',' in src_raw else src_raw
        is_valid, target_type = self._is_valid_target(dst, src)
        if not is_valid:
            return
    
        if has_console:
            admin_status = " (ADMIN)" if self._is_admin(src) else ""
            group_status = " [Groups: ON]" if self.group_responses_enabled else " [Groups: OFF]"
            print(f"📋 CommandHandler: Valid target detected - {dst} ({target_type})")
            
        msg_text = message_data.get('msg', '')
        msg_text = re.sub(r'\{\d{3}$', '', msg_text)


        # Filter for messages directed to us or valid groups  
        dst = message_data.get('dst')
        if not dst:  # Also check dst exists
            return
    
        is_valid, target_type = self._is_valid_target(dst, src)
        if not is_valid:
            return

        # Determine where to send the response
        if target_type == 'callsign':
            response_target = src  # Reply to sender
        elif target_type == 'group':
            response_target = dst  # Reply to group
        else:
            response_target = src  # Fallback
    
        if has_console:
            print(f"📋 CommandHandler: Response will be sent to {response_target} ({target_type})")

        
        # Check if message contains a command
        if not msg_text.startswith('!'):
            print(f"📋❌ CommandHandler: command doesn't start with '!'")
            return

        # ERWEITERTE FILTER LOGIK für !wx DK5EN .. damit nicht all losquaken
        # 1. Direkte Commands an uns: dst = my_callsign
        # 2. Gruppen-Commands: dst = numerische Gruppe UND message erwähnt uns
        is_direct_command = (dst == self.my_callsign)
        is_group_command = (dst and dst.isdigit() and self.my_callsign.upper() in msg_text.upper())

        if has_console:
            print(f"📋 CommandHandler: Direktbefehl {is_direct_command} oder Gruppen {is_group_command}")
            print(f"{dst} {dst.isdigit()} {self.my_callsign} {msg_text}")
    
        if not (is_direct_command or is_group_command):
            print(f"📋❌ CommandHandler: neither group or direct command detected, aborting command handling")
            return  # Nicht für uns bestimmt
            
        if has_console:
            print(f"📋 CommandHandler: Processing command '{msg_text}' from {src} to {dst}")

        if self._is_user_blocked(src):
            if has_console:
                print(f"🔴 CommandHandler: User {src} is blocked due to abuse")
            if src not in self.block_notifications_sent:
                self.block_notifications_sent.add(src)
                #await self.send_response("🚫 {src} temporarily in timeout due to repeated invalid commands", src, src_type)
                await self.send_response("🚫 {src} temporarily in timeout due to repeated invalid commands", response_target, src_type)
                if has_console:
                    print(f"🔴 CommandHandler: Sent block notification to {src}")
            else:
                if has_console:
                    print(f"🔴 CommandHandler: User {src} blocked - notification already sent, ignoring silently")
            return

        msg_id = message_data.get('msg_id')
        if self._is_duplicate_msg_id(msg_id):
            if has_console:
                print(f"🔄 CommandHandler: Duplicate msg_id {msg_id}, src_type {src_type}, ignoring silently")
            return

        content_hash = self._get_content_hash(src, msg_text, dst)
        if self._is_throttled(content_hash):
            if has_console:
                print(f"⏳ CommandHandler: THROTTLED - {src} command '{msg_text}' in group {dst} (hash: {content_hash})")
            await self.send_response("⏳ Command throttled. Same command allowed once per 5min", src, src_type)
            return

            
        # Parse and execute command
        try:
            cmd_result = self.parse_command(msg_text)
            if cmd_result:
                cmd, kwargs = cmd_result
                
                content_hash = self._get_content_hash(src, msg_text, dst)
                if self._is_throttled(content_hash, cmd):
                    if has_console:
                        timeout = COMMAND_THROTTLING.get(cmd, DEFAULT_THROTTLE_TIMEOUT)
                        print(f"⏳ THROTTLED - {src} command '!{cmd}' (timeout: {timeout}s)")
                    
                    if cmd in COMMAND_THROTTLING:
                        timeout_text = f"{COMMAND_THROTTLING[cmd]}s"
                    else:
                        timeout_text = "10min"
                        
                    await self.send_response(f"⏳ !{cmd} throttled. Try again in {timeout_text}", response_target, src_type)
                    return

                response = await self.execute_command(cmd, kwargs, src)

                self._mark_msg_id_processed(msg_id)
                self._mark_content_processed(content_hash, cmd)

                #await self.send_response(response, src, src_type)
                await self.send_response(response, response_target, src_type)

            else:
                # Track failed attempt
                self._track_failed_attempt(src)
                
                # Still mark msg_id as processed to prevent retries
                self._mark_msg_id_processed(msg_id)
                self._mark_content_processed(content_hash, cmd)

                #await self.send_response("❌ Unknown command. Try !help", src, src_type)
                await self.send_response("❌ Unknown command. Try !help", response_target, src_type)
                
        except Exception as e:

            error_type = type(e).__name__
            if has_console:
               print(f"CommandHandler ERROR ({error_type}): {e}")

            # Spezielle Behandlung für Weather-Fehler
            if 'weather' in str(e).lower():
                print(f"🌤️  Weather service issue detected: {e}")

            # Track failed attempt
            self._track_failed_attempt(src)
            
            # Mark as processed even on error
            self._mark_msg_id_processed(msg_id)
            self._mark_content_processed(content_hash, cmd)

            if 'timeout' in str(e).lower():
                await self.send_response("❌ Weather timeout. Try again later", response_target, src_type)
            elif 'weather' in str(e).lower():
                await self.send_response("❌ Weather service temporarily unavailable", response_target, src_type)
            else:
                await self.send_response(f"❌ Command failed: {str(e)[:50]}", response_target, src_type)


    def _get_content_hash(self, src, msg_text, dst=None):
        """Create hash from source + command (without arguments for command-specific throttling)"""
        # Extract command for specific throttling
        if msg_text.startswith('!'):
            parts = msg_text[1:].split()
            if parts:
                command = parts[0].lower()
                # For commands with specific throttling, use command-only hash
                if command in COMMAND_THROTTLING:
                    if dst:
                        content = f"{src}:{dst}:!{command}"
                    else:
                        content = f"{src}:!{command}"
                else:
                    if dst:
                        content = f"{src}:{dst}:{msg_text}" 
                    else:
                        content = f"{src}:{msg_text}"  # Full command + args for others
            else:
                content = f"{src}:{msg_text}"
        else:
            content = f"{src}:{msg_text}"
        
        hash_value = hashlib.md5(content.encode()).hexdigest()[:8]
        if has_console:
            print(f"🔍 Hash generation: '{content}' -> {hash_value}")
    
        return hash_value


    def _is_duplicate_msg_id(self, msg_id):
        """Check msg_id cache and cleanup expired entries"""
        current_time = time.time()
        self._cleanup_msg_id_cache(current_time)
        return msg_id in self.processed_msg_ids


    def _is_throttled(self, content_hash, command=None):
        """Check throttle cache and cleanup expired entries"""
        current_time = time.time()
        self._cleanup_throttle_cache(current_time)
        return content_hash in self.command_throttle
    
    def _cleanup_throttle_cache(self, current_time, timeout=None):
        """Remove old entries from throttle cache with specific timeout"""
        if timeout is None:
            timeout = DEFAULT_THROTTLE_TIMEOUT
            
        cutoff = current_time - timeout
        expired = [chash for chash, timestamp in self.command_throttle.items() 
                   if timestamp < cutoff]
        for chash in expired:
            del self.command_throttle[chash]

    def _is_user_blocked(self, src):
        """Check if user is blocked and cleanup expired blocks"""
        current_time = time.time()
        self._cleanup_blocked_users(current_time)
        return src in self.blocked_users

    def _mark_msg_id_processed(self, msg_id):
        """Mark msg_id as processed"""
        self.processed_msg_ids[msg_id] = time.time()

    def _mark_content_processed(self, content_hash, command=None):
        """Mark content hash as processed with command-aware timestamp"""
        # Store both timestamp and command info for cleanup
        self.command_throttle[content_hash] = {
            'timestamp': time.time(),
            'command': command
        }


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

    def _cleanup_throttle_cache(self, current_time, timeout=None):
        """Remove old entries from throttle cache with specific timeout"""
        if has_console:
            print(f"🔍 Cleanup throttle cache at {current_time}")

        expired = []
        
        for chash, data in self.command_throttle.items():
            if isinstance(data, dict):
                timestamp = data['timestamp']
                cmd = data.get('command')
            else:
                # Backward compatibility für alte float timestamps
                timestamp = data
                cmd = None
                
            # Determine timeout for this entry
            if cmd and cmd in COMMAND_THROTTLING:
                entry_timeout = COMMAND_THROTTLING[cmd]
            else:
                entry_timeout = DEFAULT_THROTTLE_TIMEOUT
        
            age = current_time - timestamp
        
            if has_console:
                print(f"🔍   Entry hash:{chash} cmd:{cmd} age:{age:.1f}s timeout:{entry_timeout}s -> {'EXPIRED' if age > entry_timeout else 'VALID'}")
            
            if age > entry_timeout:
                expired.append(chash)

        for chash in expired:
            del self.command_throttle[chash]
            if has_console:
                print(f"🔍   Removed expired hash:{chash}")

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
                if cmd in ['s', 'search'] and not kwargs:
                    kwargs['call'] = part

                elif cmd == 'pos' and not kwargs:
                    kwargs['call'] = part

                elif cmd == 'stats' and not kwargs:
                    try:
                        kwargs['hours'] = int(part)
                    except ValueError:
                        pass

                elif cmd in ['mh', 'mheard'] and not kwargs:
                    try:
                        kwargs['limit'] = int(part)
                    except ValueError:
                        if part.lower() in ['msg', 'pos', 'all']:
                            kwargs['type'] = part.lower()
                        else:
                            pass

                elif cmd == 'group' and not kwargs:
                    kwargs['state'] = part
                        
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


    async def handle_dice(self, kwargs, requester):
        """Roll two dice with Mäxchen rules"""
        # Roll two dice
        die1 = random.randint(1, 6)
        die2 = random.randint(1, 6)
        
        # Apply Mäxchen sorting rules
        sorted_value, description = self._calculate_maexchen_value(die1, die2)
        
        return f"🎲 {requester}: [{die1}][{die2}] → {sorted_value} {description}"
    
    def _calculate_maexchen_value(self, die1, die2):
        """Calculate Mäxchen value and description according to rules"""
        # Sort dice for easier processing
        dice = sorted([die1, die2], reverse=True)
        higher, lower = dice[0], dice[1]
        
        # Special case: Mäxchen (2,1)
        if set([die1, die2]) == {2, 1}:
            return "21", "(Mäxchen! 🏆)"
        
        # Double values (Pasch)
        if die1 == die2:
            pasch_names = {
                6: "Sechser-Pasch",
                5: "Fünfer-Pasch", 
                4: "Vierer-Pasch",
                3: "Dreier-Pasch",
                2: "Zweier-Pasch",
                1: "Einser-Pasch"
            }
            return f"{die1}{die2}", f"({pasch_names[die1]})"
        
        # Regular values (higher die first)
        value = f"{higher}{lower}"
        return value, ""
    
    async def handle_time(self, kwargs, requester):
        """Show current time and date"""
        now = datetime.now()
        
        # German format
        date_str = now.strftime("%d.%m.%Y")
        time_str = now.strftime("%H:%M:%S")
        weekday = now.strftime("%A")
        
        # German weekday names
        weekday_german = {
            "Monday": "Montag",
            "Tuesday": "Dienstag", 
            "Wednesday": "Mittwoch",
            "Thursday": "Donnerstag",
            "Friday": "Freitag",
            "Saturday": "Samstag",
            "Sunday": "Sonntag"
        }
        
        weekday_de = weekday_german.get(weekday, weekday)
        
        return f"🕐 {time_str} Uhr, {weekday_de}, {date_str}"
    

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
        """Show recently heard stations with optional type filtering"""
        limit = int(kwargs.get('limit', 5))
        msg_type = kwargs.get('type', 'all').lower()
        
        if not self.storage_handler:
            return "❌ Message storage not available"
            
        # Collect station data
        stations = defaultdict(lambda: {'last_msg': 0, 'msg_count': 0, 'last_pos': 0, 'pos_count': 0})
        
        for item in list(self.storage_handler.message_store)[-4000:]:
            try:
                raw_data = json.loads(item["raw"])
                data_type = raw_data.get('type', '')
                src = raw_data.get('src', '')
                timestamp = raw_data.get('timestamp', 0)
                
                if data_type not in ['msg', 'pos'] or not src:
                    continue
                    
                call = src.split(',')[0]
                
                if data_type == 'msg':
                    stations[call]['msg_count'] += 1
                    if timestamp > stations[call]['last_msg']:
                        stations[call]['last_msg'] = timestamp
                elif data_type == 'pos':
                    stations[call]['pos_count'] += 1
                    if timestamp > stations[call]['last_pos']:
                        stations[call]['last_pos'] = timestamp
                        
            except (json.JSONDecodeError, KeyError):
                continue
        
        # Build response lines
        lines = []
        
        if msg_type in ['all', 'msg']:
            msg_stations = [(call, data['msg_count'], data['last_msg']) 
                           for call, data in stations.items() if data['msg_count'] > 0]
            if msg_stations:
                msg_stations.sort(key=lambda x: x[2], reverse=True)
                msg_entries = [f"{call} @{time.strftime('%H:%M', time.localtime(ts/1000))} ({count})" 
                              for call, count, ts in msg_stations[:limit]]
                lines.append("📻 MH: 💬 " + " | ".join(msg_entries))
        
        if msg_type in ['all', 'pos']:
            pos_stations = [(call, data['pos_count'], data['last_pos']) 
                           for call, data in stations.items() if data['pos_count'] > 0]
            if pos_stations:
                pos_stations.sort(key=lambda x: x[2], reverse=True)
                pos_entries = [f"{call} @{time.strftime('%H:%M', time.localtime(ts/1000))} ({count})" 
                              for call, count, ts in pos_stations[:limit]]
                lines.append("      📍 " + " | ".join(pos_entries))
        
        if not lines:
            return "📻 No activity found"
        
        # Join lines with padding separator for chunking
        if len(lines) == 1:
            return lines[0]
        else:
            # Pad first line to force chunk break
            line1 = lines[0]
            padding_needed = max(0, 138 - len(line1.encode('utf-8')))
            return line1 + " " * padding_needed + ", " + lines[1]


    def _pad_for_chunk_break(self, text, target_length=MAX_RESPONSE_LENGTH-2):
        """Pad text to force clean chunk boundary using byte-aware calculation"""
        text_bytes = text.encode('utf-8')
        
        if len(text_bytes) < target_length:
            # Calculate padding needed in bytes
            padding_needed = target_length - len(text_bytes)
            # Use spaces for padding (1 byte each)
            padded_text = text + " " * padding_needed + ", "
        else:
            # Text is already at or over target, just add separator
            padded_text = text + ", "
        
        if has_console:
            original_bytes = len(text.encode('utf-8'))
            padded_bytes = len(padded_text.encode('utf-8'))
            print(f"🔍 Padding: '{text[:30]}...' {original_bytes}→{padded_bytes} bytes")
        
        return padded_text


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
        
        # Group commands by category
        search_cmds = ["!search user:CALL days:7", "!pos call:CALL"]
        stats_cmds = ["!stats 24", "!mheard 5"] 
        weather_cmds = ["!wx"] 
        fun_cmds = ["!dice", "!time"]
        
        response += "Search: " + ", ".join(search_cmds) + " | "
        response += "Stats: " + ", ".join(stats_cmds) + " | "
        response += "Weather: " + ", ".join(weather_cmds) + " | "
        response += "Fun: " + ", ".join(fun_cmds)
        
        return response


    async def send_response(self, response, recipient, src_type='udp'):
        """Send response back to requester, chunking if necessary"""
        if not response:
            return

        if has_console:
             print(f"🐛 send_response: recipient='{recipient}', my_callsign='{self.my_callsign}', equal={recipient.upper() == self.my_callsign}")


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
                  if has_console:
                     print("command handler: src_type",src_type)

                  try:
                        if src_type=="ble":
                            await self.message_router.publish('command', 'ble_message', message_data)
                            if has_console:
                                print(f"📋 CommandHandler: Sent chunk {i+1} via BLE to {recipient}")
                        elif src_type in ["udp", "node", "lora"]:
                                # Update message data for UDP transport
                                message_data['src_type'] = 'command_response_udp'
                                await self.message_router.publish('command', 'udp_message', message_data)
                                if has_console:
                                    print(f"📋 CommandHandler: Sent chunk {i+1} via UDP to {recipient}")
                        else:
                            print("TransportUnavailableError BLE and UDP not available",src_type)
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
        """Split response into chunks - simple and robust"""
        max_bytes = MAX_RESPONSE_LENGTH
        
        # Single chunk fits?
        if len(response.encode('utf-8')) <= max_bytes:
            return [response]
        
        chunks = []
        
        # Split on padding separator first (for our two-line responses)
        if ', ' in response and len(response.split(', ')) == 2:
            chunks = response.split(', ')
        else:
            # Split long single responses on station boundaries
            if ' | ' in response:
                parts = response.split(' | ')
                current = ""
                
                for part in parts:
                    test = current + (" | " if current else "") + part
                    if len(test.encode('utf-8')) <= max_bytes:
                        current = test
                    else:
                        if current:
                            chunks.append(current)
                        current = part
                
                if current:
                    chunks.append(current)
            else:
                # Fallback: character-wise split
                chunks = [response[i:i+max_bytes] for i in range(0, len(response), max_bytes)]
        
        return chunks[:MAX_CHUNKS]


# Integration function for your main script
def create_command_handler(message_router, storage_handler, call_sign, lat, long, stat_name):
    """Factory function to create and integrate CommandHandler"""
    return CommandHandler(message_router, storage_handler, call_sign, lat, long, stat_name)
