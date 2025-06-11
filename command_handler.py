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
from typing import Dict, Optional

VERSION="v0.51.0"

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
    'userinfo': {
        'handler': 'handle_userinfo',
        'args': [],
        'format': '!userinfo',
        'description': 'Show user information'
    },
    'kb': {
        'handler': 'handle_kickban',
        'args': ['callsign', 'action'],
        'format': '!kb [callsign] [del|list|delall]',
        'description': 'Manage blocked callsigns (admin only)'
    },
    'topic': {
        'handler': 'handle_topic',
        'args': ['group', 'text', 'interval'],
        'format': '!topic [group] [text] [interval:minutes] | !topic | !topic delete group',
        'description': 'Manage group beacon messages (admin only)'
    },
    'ctcping': {
        'handler': 'handle_ctcping',
        'args': ['call', 'payload', 'repeat'],
        'format': '!ctcping call:TARGET payload:20 repeat:3',
        'description': 'Ping test with roundtrip time measurement'
    },
    'help': {
        'handler': 'handle_help',
        'args': [],
        'format': '!help',
        'description': 'Show available commands'
    }
}


class CommandHandler:
    def __init__(self, message_router=None, storage_handler=None, my_callsign = "DK0XXX", lat = 48.4031, lon = 11.7497, stat_name = "Freising", user_info_text=None):
        self.blocked_callsigns = set()

        # Topic/Beacon system - NEUE ZEILEN:
        self.active_topics = {}  # {group: {'text': str, 'interval': int, 'task': asyncio.Task}}
        self.topic_tasks = set() 

        # CTC Ping system - NEUE ZEILEN:
        self.active_pings = {}  # {ping_id: PingTest}
        self.ping_tests = {}
        self.ping_timeout = 30.0  # 30 seconds per ping

        self.message_router = message_router
        self.storage_handler = storage_handler
        self.my_callsign = my_callsign.upper()  # Your callsign to filter commands
        self.admin_callsign_base = my_callsign.split('-')[0]
        self.lat = lat
        self.lon = lon
        self.stat_name = stat_name
        self.user_info_text = user_info_text or f"{my_callsign} Node | No additional info configured"
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
        self.throttle_timeout = DEFAULT_THROTTLE_TIMEOUT 
        
        # Abuse protection
        self.failed_attempts = {}  # {src: [timestamp, timestamp, ...]}
        self.max_failed_attempts = 3
        self.failed_attempt_window = DEFAULT_THROTTLE_TIMEOUT
        self.block_duration = 5 * DEFAULT_THROTTLE_TIMEOUT
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



    def normalize_command_data(self, message_data):
        """Normalize command data with uppercase conversion"""
        src_raw = message_data.get('src', 'UNKNOWN')
        src = src_raw.split(',')[0].strip().upper() if ',' in src_raw else src_raw.strip().upper()
        
        dst = message_data.get('dst', '').strip().upper()
        msg = message_data.get('msg', '').strip()
        
        # Commands to uppercase
        if msg.startswith('!'):
            msg = msg.upper()
        
        return {
            'src': src,
            'dst': dst, 
            'msg': msg,
            'original': message_data
        }

    def _should_execute_command(self, src, dst, msg):
        """Simplified reception logic from table"""
        src = src.upper()
        dst = dst.upper() 
        msg = msg.upper()

        if has_console:
            print(f"🔍 Command execution check: src='{src}', dst='{dst}', msg='{msg[:20]}...'")
        
        # Invalid destinations never execute
        if dst in ['*', 'ALL', '']:
            if has_console:
                print(f"🔍 → Invalid dst '{dst}' - NO EXECUTION")
            return False, None
        
        target = self.extract_target_callsign(msg)
        
        # Target must be us
        if target != self.my_callsign:
            if has_console:
                print(f"🔍 → Target '{target}' != us ({self.my_callsign}) - NO EXECUTION")
            return False, None
        
        # Direct to us → always OK
        if dst == self.my_callsign:
            if has_console:
                print(f"🔍 → Direct message to us - EXECUTE")
            return True, 'direct'
        
        # Group message → check permissions
        if self.is_group(dst):
            execute = self.group_responses_enabled or self._is_admin(src)
            reason = "Groups ON" if self.group_responses_enabled else "Admin override" if self._is_admin(src) else "Groups OFF"
            if has_console:
                print(f"🔍 → Group '{dst}' - {'EXECUTE' if execute else 'NO EXECUTION'} ({reason})")

            if execute:
                return True, 'group'
            else:
                return False, None  # ← This was the bug: was returning (False, 'group')
    
        if has_console:
            print(f"🔍 → No match - NO EXECUTION")
        return False, None

    def extract_target_callsign(self, msg):
        """Extract target callsign from command message"""
        if not msg or not msg.startswith('!'):
            return None
        
        # Ensure message is uppercase for processing
        msg_upper = msg.upper().strip()
        parts = msg_upper.split()
        
        if len(parts) < 2:
            return None
        
        # Look for target in last part (pattern: !WX DK5EN-15)
        potential_target = parts[-1].strip()
        
        # Validate callsign pattern
        if re.match(r'^[A-Z]{1,2}[0-9][A-Z]{1,3}(-\d{1,2})?$', potential_target):
            return potential_target
        
        return None

    def is_group(self, dst):
        """Check if destination is a group"""
        if not dst:
            return False
    
        # Special group 'TEST'
        if dst.upper() == 'TEST':
            return True
   
        # Numeric groups: 1-99999
        if dst.isdigit():
            try: 
                group_num = int(dst)
                return 1 <= group_num <= 99999
            except ValueError:
                return False
    
        return False


    def _is_admin(self, callsign):
        """Check if callsign is admin (DK5EN with any SID)"""
        if not callsign:
            return False
        base_call = callsign.split('-')[0] if '-' in callsign else callsign
        return base_call.upper() == self.admin_callsign_base.upper()

    async def _message_handler(self, routed_message):
        """Handle incoming messages and check for commands"""
        message_data = routed_message['data']
        src_type = message_data.get('src_type')

        if 'msg' not in message_data:
            return

        msg_text = message_data.get('msg', '')

        if self._is_echo_message(msg_text):
            await self._handle_echo_message(message_data)
            return 

        if self._is_ack_message(msg_text):
            await self._handle_ack_message(message_data)
            return

        if not msg_text or not msg_text.startswith('!'):
            return
      
        msg_text = re.sub(r'\{\d+$', '', msg_text)  # Remove {829 at end

        msg_id = message_data.get('msg_id')
        if self._is_duplicate_msg_id(msg_id):
            if has_console:
                print(f"🔄 CommandHandler: Duplicate msg_id {msg_id}, ignoring silently")
            return

        # EARLY NORMALIZATION using the same pattern as MessageRouter
        normalized = self.normalize_command_data(message_data)
        src = normalized['src']
        dst = normalized['dst'] 
        msg_text = normalized['msg']

        if has_console:
            print(f"📋 CommandHandler: Checking command '{msg_text}' from {src} to {dst}")

        # NEW: Use simplified reception logic
        should_execute, target_type = self._should_execute_command(src, dst, msg_text)
        
        if not should_execute:
            if has_console:
                print(f"📋 CommandHandler: Command execution denied")
            return

        if has_console:
            admin_status = " (ADMIN)" if self._is_admin(src) else ""
            group_status = " [Groups: ON]" if self.group_responses_enabled else " [Groups: OFF]"
            print(f"📋 CommandHandler: Executing {target_type} command{admin_status}{group_status}")

        # Determine response target
        if target_type == 'direct':
            response_target = src  # Reply to sender
        else:
            response_target = dst  # Reply to group
        
        if has_console:
            print(f"📋 CommandHandler: Response will be sent to {response_target} ({target_type})")

        # Check if user is blocked
        if self._is_user_blocked(src):
            if has_console:
                print(f"🔴 CommandHandler: User {src} is blocked due to abuse")
            if src not in self.block_notifications_sent:
                self.block_notifications_sent.add(src)
                await self.send_response("🚫 Temporarily in timeout due to repeated invalid commands", response_target, src_type)
            return

        # Check throttling
        content_hash = self._get_content_hash(src, msg_text, dst)
        if self._is_throttled(content_hash):
            if has_console:
                print(f"⏳ CommandHandler: THROTTLED - {src} command '{msg_text}'")
            await self.send_response("⏳ Command throttled. Same command allowed once per 5min", response_target, src_type)
            return
                
        # Parse and execute command
        try:
            cmd_result = self.parse_command(msg_text)
            if cmd_result:
                cmd, kwargs = cmd_result
                
                if self._is_throttled(content_hash, cmd):
                    timeout_text = f"{COMMAND_THROTTLING.get(cmd, DEFAULT_THROTTLE_TIMEOUT//60)}min"
                    await self.send_response(f"⏳ !{cmd} throttled. Try again in {timeout_text}", response_target, src_type)
                    return

                response = await self.execute_command(cmd, kwargs, src)

                self._mark_msg_id_processed(msg_id)
                self._mark_content_processed(content_hash, cmd)

                await self.send_response(response, response_target, src_type)

            else:
                # Track failed attempt
                self._track_failed_attempt(src)
                self._mark_msg_id_processed(msg_id)
                await self.send_response("❌ Unknown command. Try !help", response_target, src_type)
                    
        except Exception as e:
            error_type = type(e).__name__
            if has_console:
               print(f"CommandHandler ERROR ({error_type}): {e}")

            self._track_failed_attempt(src)
            self._mark_msg_id_processed(msg_id)

            if 'timeout' in str(e).lower():
                await self.send_response("❌ Command timeout. Try again later", response_target, src_type)
            elif 'weather' in str(e).lower():
                await self.send_response("❌ Weather service temporarily unavailable", response_target, src_type)
            else:
                await self.send_response(f"❌ Command failed: {str(e)[:50]}", response_target, src_type)


    def _is_ack_message(self, msg: str) -> bool:
        """Check if message is an ACK with :ackXXX pattern"""
        if not msg:
            return False
    
        # Pattern: "CALLSIGN :ackXXX" or "CALLSIGN  :ackXXX" (allow multiple spaces)
        pattern = r'\s+:ack\d{3}$'
        return bool(re.search(pattern, msg))



    async def _handle_ack_message(self, message_data: dict):
        """Handle ACK message and calculate RTT"""
        try:
            src = message_data.get('src', '').upper()
            dst = message_data.get('dst', '').upper()
            msg = message_data.get('msg', '')
            
            # Extract ACK ID from message
            # Format: "DK5EN-1 :ack753" or "DK5EN-1  :ack753"
            match = re.search(r'\s+:ack(\d{3})$', msg)
            if not match:
                return
            
            ack_id = match.group(1)  # e.g., "753"
            
            # Check if we have a matching ping
            if ack_id not in self.active_pings:
                if has_console:
                    print(f"🏓 Received ACK {ack_id} from {src}, but no matching ping found")
                return
            
            ping_info = self.active_pings[ack_id]
            
            # Verify the ACK comes from the expected target
            if src != ping_info['target'] or dst != self.my_callsign:
                if has_console:
                    print(f"🏓 ACK {ack_id} from {src} or {dst}, but expected from {ping_info['target']}")
                    print(f"🏓 ACK {ack_id} not directed to us (dst={dst})")
                return
            
            # Calculate RTT
            receive_time = time.time()
            sent_time = ping_info['sent_time']
            rtt = receive_time - sent_time

            # Create individual result
            individual_result = {
                'sequence': ping_info.get('sequence_info', ''),
                'rtt': rtt,
                'status': 'success',
                'timestamp': receive_time
            }

            test_id = ping_info.get('test_id')

            if test_id and test_id in self.ping_tests:
                test_summary = self.ping_tests[test_id]
                test_summary['results'].append(individual_result)
                test_summary['completed'] += 1

            
            # Remove from active pings (ACK received)
            del self.active_pings[ack_id]
            
            # Send result to requester
            target = ping_info['target']
            requester = ping_info['requester']

            # Create result message
            rtt_ms = rtt * 1000  # Convert to milliseconds

            result_msg = f"🏓 Ping to {target}: RTT = {rtt_ms:.1f}ms"
            if ping_info.get('sequence_info'):
                result_msg = f"🏓 Ping {ping_info['sequence_info']} to {target}: RTT = {rtt_ms:.1f}ms"
        
            
            await self._send_ping_result(requester, result_msg)
            
            if has_console:
                print(f"🏓 ACK received: ID={ack_id}, target={target}, RTT={rtt_ms:.1f}ms")
                
        except Exception as e:
            if has_console:
                print(f"❌ Error handling ACK message: {e}")






    def _is_echo_message(self, msg: str) -> bool:
        """Check if message is an echo with {xxx} suffix"""
        if not msg:
            return False
    
        # Check for {xxx} pattern at the end
        pattern = r'\{\d{3}$'  # Exactly 3 digits after {
        return bool(re.search(pattern, msg))



    async def _handle_echo_message(self, message_data: dict):
        """Handle echo message and start tracking for ACK"""
        try:
            src = message_data.get('src', '').upper()
            dst = message_data.get('dst', '').upper()  
            msg = message_data.get('msg', '')
            
            # Extract message ID from {xxx} suffix
            match = re.search(r'\{(\d{3})$', msg)
            if not match:
                return
            
            message_id = match.group(1)  # e.g., "753"
            original_msg = msg[:-4]  # Remove {753 suffix
            
            # Only track echoes from our own messages
            if src != self.my_callsign:
                return
            
            # Check if this looks like a ping message
            if not self._is_ping_message(original_msg):
                return

            sequence_info = self._extract_sequence_info(original_msg)
            test_id = self._find_test_id_for_target(dst)
            
            # Store ping tracking info
            ping_info = {
                'target': dst,
                'original_msg': original_msg,
                'sent_time': time.time(),  # Time when we received echo
                'requester': src,  # Should be us
                'status': 'waiting_ack',
                'sequence_info': sequence_info,
                'test_id': test_id  
            }
            
            self.active_pings[message_id] = ping_info
            
            if has_console:
                seq_text = f" ({sequence_info})" if sequence_info else ""
            
                print(f"🏓 Echo received: ID={message_id}, target={dst}, waiting for ACK...")
                
            # Start timeout task
            asyncio.create_task(self._ping_timeout_task(message_id))
            
        except Exception as e:
            if has_console:
                print(f"❌ Error handling echo message: {e}")
    

    def _extract_sequence_info(self, msg: str) -> Optional[str]:
        """Extract sequence info from ping message"""
        # Look for "ping test X/Y" pattern
        match = re.search(r'ping test (\d+)/(\d+)', msg.lower())
        if match:
            current = match.group(1)
            total = match.group(2)
            return f"{current}/{total}"
        return None


    # Ping-Message Detection:
    def _is_ping_message(self, msg: str) -> bool:
        """Check if message looks like a ping test message"""
        if not msg:
            return False

        msg_lower = msg.lower()

        # Must contain "ping test" AND measurement-related terms
        has_ping_test = "ping test" in msg_lower
        has_measurement = any(term in msg_lower for term in [
            "to measure roundtrip",
            "measure round trip", 
            "roundtrip"
        ])

        return has_ping_test and has_measurement

        
    
    # Timeout-Task für Pings:
    async def _ping_timeout_task(self, message_id: str):
        """Handle ping timeout after 30 seconds"""
        try:
            await asyncio.sleep(self.ping_timeout)  # 30 seconds
            
            # Check if ping is still active (not received ACK)
            if message_id in self.active_pings:
                ping_info = self.active_pings[message_id]
                
                if ping_info['status'] == 'waiting_ack':
                    # Timeout occurred
                    target = ping_info['target']
                    requester = ping_info['requester']
                    sequence_info = ping_info.get('sequence_info', '')
                    test_id = ping_info.get('test_id')

                    # Update test summary
                    if test_id and test_id in self.ping_tests:
                        test_summary = self.ping_tests[test_id]
                        test_summary['timeouts'] += 1
                    
                        # Add timeout result
                        timeout_result = {
                            'sequence': sequence_info,
                            'rtt': None,
                            'status': 'timeout',
                            'timestamp': time.time()
                        }
                        test_summary['results'].append(timeout_result)
                    
                    # Remove from active pings
                    del self.active_pings[message_id]
                    
                    # Send timeout result
                    seq_text = f" {sequence_info}" if sequence_info else ""
                
                    timeout_msg = f"🏓 Ping to {target}: timeout (no ACK received after 30s)"
                    await self._send_ping_result(requester, timeout_msg)
                    
                    if has_console:
                        print(f"⏰ Ping timeout: ID={message_id}, target={target}{seq_text}")
                    
                        
        except asyncio.CancelledError:
            # Task was cancelled (probably because ACK was received)
            pass
        except Exception as e:
            if has_console:
                print(f"❌ Error in ping timeout task: {e}")



    def _find_test_id_for_target(self, target: str) -> Optional[str]:
        """Find active test ID for target"""
        for test_id, test_info in self.ping_tests.items():
            if test_info['target'] == target and test_info['status'] == 'running':
                return test_id
        return None

    
    # Debug-Methode für aktive Pings:
    def get_active_pings_info(self) -> str:
        """Get info about currently active pings (for debugging)"""
        if not self.active_pings:
            return "No active pings"
        
        ping_info = []
        for msg_id, info in self.active_pings.items():
            target = info['target']
            status = info['status']
            elapsed = time.time() - info['sent_time']
            seq_info = info.get('sequence_info', '')

            seq_text = f" {seq_info}" if seq_info else ""
            ping_info.append(f"ID:{msg_id}{seq_text} → {target} ({status}, {elapsed:.1f}s)")
    
        
        return f"Active pings: {' | '.join(ping_info)}"


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
        if has_console:
                print(f"🔍 valid_target dubug {dst}, {src}")

        # Always allow direct messages to our callsign
        if dst.upper() == self.my_callsign.upper():
            if has_console:
                print(f"🔍 valid_target Ture, callsign")
            return True, 'callsign'
        
        # Check if dst is a valid group format
        is_valid_group = dst == 'TEST' or (dst and dst.isdigit() and 1 <= len(dst) <= 5)
        if not is_valid_group:
            if has_console:
                print(f"🔍 valid_target False, None")
            return False, None
        
        # Admin always allowed for groups
        if self._is_admin(src):
            if has_console:
                print(f"🔍 valid_target admin override, True, group")
            return True, 'group'
        
        # Non-admin only allowed if group responses are enabled
        if self.group_responses_enabled:
            if has_console:
                print(f"🔍 valid_target group responses enabled, True, group")
            return True, 'group'
        
        if has_console:
                print(f"🔍 valid_target no match, False, None")
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

                elif cmd == 'ctcping' and not kwargs:
                    # Handle ctcping arguments: !ctcping call:TARGET payload:20 repeat:3
                    for part in parts[1:]:
                        if ':' in part:
                            key, value = part.split(':', 1)
                            key = key.lower()
                            if key == 'call':
                                kwargs['call'] = value.upper()
                            elif key == 'payload':
                                try:
                                    kwargs['payload'] = int(value)
                                except ValueError:
                                    pass
                            elif key == 'repeat':
                                try:
                                    kwargs['repeat'] = int(value)
                                except ValueError:
                                    pass

                elif cmd == 'topic' and not kwargs:
                    # Handle topic arguments: !topic [group] [text] [interval] | !topic delete group
                    if len(parts) >= 2:
                        if parts[1].upper() == 'DELETE' and len(parts) >= 3:
                            kwargs['action'] = 'delete'
                            kwargs['group'] = parts[2].upper()
                        else:
                            # Parse: !topic GROUP "beacon text" interval:30
                            kwargs['group'] = parts[1].upper()
            
                            if len(parts) >= 3:
                                # Find text (everything between group and last interval part)
                                text_parts = []
                                interval_part = None
                
                                for i, part in enumerate(parts[2:], 2):
                                    if ':' in part and part.startswith('interval:'):
                                        interval_part = part
                                        break
                                    else:
                                        text_parts.append(parts[i])
                
                                if text_parts:
                                    kwargs['text'] = ' '.join(text_parts)
                
                                if interval_part:
                                    try:
                                        interval_value = int(interval_part.split(':', 1)[1])
                                        kwargs['interval'] = interval_value
                                    except (ValueError, IndexError):
                                        pass
                                elif len(parts) >= 4 and parts[-1].isdigit():
                                    # Fallback: last part is interval without 'interval:' prefix
                                    try:
                                        kwargs['interval'] = int(parts[-1])
                                        # Remove interval from text
                                        if text_parts and text_parts[-1] == parts[-1]:
                                            text_parts = text_parts[:-1]
                                            kwargs['text'] = ' '.join(text_parts) if text_parts else kwargs.get('text', '')
                                    except ValueError:
                                        pass

                elif cmd == 'kb' and not kwargs:
                    # Handle kb arguments: !kb CALL [del|list|delall]
                    if len(parts) >= 2:
                        first_arg = parts[1].upper()
                    
                        # Check if first argument is a special command
                        if first_arg in ['LIST', 'DELALL']:
                            kwargs['callsign'] = first_arg.lower()
                        else:
                            # First argument is a callsign
                            kwargs['callsign'] = first_arg
                        
                            # Check for second argument (action)
                            if len(parts) >= 3:
                                second_arg = parts[2].upper()
                                if second_arg == 'DEL':
                                    kwargs['action'] = 'del'
                        
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






    async def handle_ctcping(self, kwargs, requester):
        """Handle CTC ping test with roundtrip time measurement"""
        target = kwargs.get('call', '').upper()
        payload_size = kwargs.get('payload', 20)  # Default 20
        repeat_count = kwargs.get('repeat', 1)    # Default 1
        
        # Validation
        if not target:
            return "❌ Target callsign required (call:TARGET)"
        
        if not re.match(r'^[A-Z]{1,2}[0-9][A-Z]{1,3}(-\d{1,2})?$', target):
            return "❌ Invalid target callsign format"
        
        if target == self.my_callsign:
            return "❌ Cannot ping yourself"
        
        try:
            payload_size = int(payload_size)
            if payload_size < 1 or payload_size > 140:
                return "❌ Payload size must be between 1 and 140 bytes"
        except (ValueError, TypeError):
            return "❌ Invalid payload size"
        
        try:
            repeat_count = int(repeat_count)
            if repeat_count < 1 or repeat_count > 5:
                return "❌ Repeat count must be between 1 and 5"
        except (ValueError, TypeError):
            return "❌ Invalid repeat count"
        
        # Check if target is blocked
        if hasattr(self, 'blocked_callsigns') and target in self.blocked_callsigns:
            return f"❌ Target {target} is blocked"
        
        # Start ping test
        await self._start_ping_test(target, payload_size, repeat_count, requester)
        
        return f"🏓 Ping test to {target} started: {repeat_count} ping(s) with {payload_size} bytes payload..."
    
    async def _start_ping_test(self, target: str, payload_size: int, repeat_count: int, requester: str):
        """Start the ping test sequence"""
        test_id = f"{target}_{int(time.time())}"

        # Initialize test summary
        test_summary = {
            'test_id': test_id,
            'target': target,
            'requester': requester,
            'total_pings': repeat_count,
            'payload_size': payload_size,
            'start_time': time.time(),
            'results': [],  # List of individual ping results
            'completed': 0,
            'timeouts': 0,
            'status': 'running'
    }

        self.ping_tests[test_id] = test_summary

        try:
            for sequence in range(1, repeat_count + 1):
                # Create ping message with sequence info
                base_msg = f"Ping test {sequence}/{repeat_count} to measure roundtrip"
                
                # Adjust message length to payload_size
                if len(base_msg) > payload_size:
                    ping_message = base_msg[:payload_size]
                elif len(base_msg) < payload_size:
                    padding = 'X' * (payload_size - len(base_msg))
                    ping_message = base_msg + padding
                else:
                    ping_message = base_msg
                
                # Send ping message
                await self._send_ping_message(target, ping_message, sequence, repeat_count, requester)
                
                # Wait between pings (except for last one)
                if sequence < repeat_count:
                    await asyncio.sleep(12.0)  

            asyncio.create_task(self._monitor_test_completion(test_id))                    

        except Exception as e:
            if has_console:
                print(f"❌ Ping test error: {e}")
            
            # Send error to requester
            test_summary['status'] = 'error'
            await self._send_ping_result(requester, f"🏓 Ping test error: {str(e)[:50]}")

    
    async def _send_ping_message(self, target: str, message: str, sequence: int, total: int, requester: str, test_id: str):
        """Send a single ping message and track it"""
        try:
            if self.message_router:
                message_data = {
                    'dst': target,
                    'msg': message,
                    'src_type': 'ctcping',
                    'type': 'msg'
                }
                
                # Send the ping message
                await self.message_router.publish('ctcping', 'udp_message', message_data)
                
                if has_console:
                    print(f"🏓 Sent ping {sequence}/{total} to {target}: '{message[:30]}...'")
                    print(f"🏓 Waiting for echo and ACK...")
                    
        except Exception as e:
            if has_console:
                print(f"❌ Failed to send ping to {target}: {e}")



    async def cleanup_ping_tests(self):
        """Clean up all active ping tests"""
        if has_console:
            print(f"🧹 Cleaning up {len(self.active_pings)} active pings...")
    
        # Clear all active pings (this will also stop timeout tasks)
        self.active_pings.clear()
        self.ping_tests.clear()
    
        if has_console:
            print("✅ All ping tests cleaned up")





    async def test_ctcping_logic(self):
        """Test CTC ping functionality with complex scenarios"""
        if has_console:
            print("\n🧪 Testing CTC Ping Logic:")
            print("=" * 45)
        
        # === Phase 1: Parameter Validation Tests ===
        validation_tests = [
            # (requester, args, expected_result_contains, description)
            ("OE1ABC-5", {}, "❌ Target callsign required", "Missing target"),
            ("OE1ABC-5", {'call': 'INVALID'}, "❌ Invalid target callsign format", "Invalid callsign format"),
            ("OE1ABC-5", {'call': self.my_callsign}, "❌ Cannot ping yourself", "Self-ping prevention"),
            ("OE1ABC-5", {'call': 'W1ABC-1', 'payload': 0}, "❌ Payload size must be between", "Payload too small"),
            ("OE1ABC-5", {'call': 'W1ABC-1', 'payload': 141}, "❌ Payload size must be between", "Payload too large"),
            ("OE1ABC-5", {'call': 'W1ABC-1', 'payload': 'invalid'}, "❌ Invalid payload size", "Invalid payload format"),
            ("OE1ABC-5", {'call': 'W1ABC-1', 'repeat': 0}, "❌ Repeat count must be between", "Repeat too small"),
            ("OE1ABC-5", {'call': 'W1ABC-1', 'repeat': 6}, "❌ Repeat count must be between", "Repeat too large"),
            ("OE1ABC-5", {'call': 'W1ABC-1', 'repeat': 'invalid'}, "❌ Invalid repeat count", "Invalid repeat format"),
        ]
        
        results = []
        
        # Clean start
        await self._cleanup_test_ctcping()
        
        # Run validation tests
        for requester, args, expected_contains, description in validation_tests:
            try:
                result = await self.handle_ctcping(args, requester)
                
                result_match = expected_contains.lower() in result.lower()
                status = "✅ PASS" if result_match else "❌ FAIL"
                
                results.append((status, description, result_match))
                
                if has_console:
                    print(f"{status} | {description}")
                    if not result_match:
                        print(f"     ❌ Expected: '{expected_contains}' in '{result}'")
                        
            except Exception as e:
                status = "❌ ERROR"
                results.append((status, description, False))
                if has_console:
                    print(f"{status} | {description} - Exception: {e}")
        
        # === Phase 2: Message Pattern Recognition Tests ===
        pattern_tests = [
            # (message, expected_result, description)
            ("Ping test 1/3 to measure roundtrip{753", True, "Echo message detection"),
            ("Ping test 2/5 to measure roundtripXXXX{052", True, "Echo with padding detection"),
            ("Normal message{123", False, "Non-ping echo ignored"),
            ("DK5EN-1  :ack753", True, "ACK message detection"),
            ("OE5HWN-12 :ack052", True, "ACK with different ID"),
            ("DK5EN-1  :ack75", False, "Invalid ACK (2 digits)"),
            ("DK5EN-1  :ack7534", False, "Invalid ACK (4 digits)"),
            ("Random message", False, "Normal message ignored"),
        ]
        
        for message, expected_result, description in pattern_tests:
            echo_result = self._is_echo_message(message)
            ack_result = self._is_ack_message(message)
            ping_result = self._is_ping_message(message.replace(r'\{\d{3}$', ''))
            
        if "echo" in description.lower():
            if "Non-ping echo ignored" in description:
                # Test complete echo filtering logic - extract original message and test if it's a ping
                original_msg = message[:-4] if message.endswith('}') and len(message) >= 4 else message
                # Remove the {123} suffix and test if the remaining message is a ping
                clean_msg = re.sub(r'\{\d{3}$', '', original_msg)
                # For "Non-ping echo ignored", we expect the message to NOT be a ping (False)
                actual_result = self._is_ping_message(clean_msg)
            else:
                # Test basic echo pattern detection
                actual_result = echo_result
        elif "ack" in description.lower():
            actual_result = ack_result
        else:
            actual_result = ping_result

            
            result_match = actual_result == expected_result
            status = "✅ PASS" if result_match else "❌ FAIL"
            
            results.append((status, description, result_match))
            
            if has_console:
                print(f"{status} | {description}")
                if not result_match:
                    print(f"     ❌ Expected: {expected_result}, Got: {actual_result}")
        
        # === Phase 3: Sequence Info Extraction Tests ===
        sequence_tests = [
            ("Ping test 1/3 to measure roundtrip", "1/3", "Single digit sequence"),
            ("Ping test 10/15 to measure roundtrip", "10/15", "Double digit sequence"),
            ("Ping test 2/5 to measure roundtripXXXX", "2/5", "Sequence with padding"),
            ("Random ping message", None, "No sequence info"),
        ]
        
        for message, expected_seq, description in sequence_tests:
            actual_seq = self._extract_sequence_info(message)
            result_match = actual_seq == expected_seq
            status = "✅ PASS" if result_match else "❌ FAIL"
            
            results.append((status, description, result_match))
            
            if has_console:
                print(f"{status} | {description}")
                if not result_match:
                    print(f"     ❌ Expected: '{expected_seq}', Got: '{actual_seq}'")
        
        # === Phase 4: Simulated Ping Flow Tests ===
        await self._test_simulated_ping_flows(results)
        
        # === Phase 5: Blocked Target Test ===
        if hasattr(self, 'blocked_callsigns'):
            old_blocked = self.blocked_callsigns.copy()
            self.blocked_callsigns.add('W1ABC-5')
            
            try:
                result = await self.handle_ctcping({'call': 'W1ABC-5'}, "OE1ABC-5")
                blocked_match = "blocked" in result.lower()
                status = "✅ PASS" if blocked_match else "❌ FAIL"
                results.append((status, "Blocked target rejection", blocked_match))
                
                if has_console:
                    print(f"{status} | Blocked target rejection")
                    if not blocked_match:
                        print(f"     ❌ Should contain 'blocked' in '{result}'")
            finally:
                self.blocked_callsigns = old_blocked
        
        # === Summary ===
        await self._cleanup_test_ctcping()
        
        passed = sum(1 for r in results if r[2])
        total = len(results)
        
        if has_console:
            print(f"\n🧪 CTC Ping Test Summary: {passed}/{total} tests passed")
            if passed == total:
                print("🎉 All CTC ping tests passed!")
            else:
                print("⚠️ Some CTC ping tests failed!")
                
                failed_tests = [r for r in results if not r[2]]
                if failed_tests:
                    print("\n❌ Failed Tests:")
                    for status, description, _ in failed_tests:
                        print(f"   • {description}")
            
            print("=" * 45)
        
        return passed == total
    
    async def _test_simulated_ping_flows(self, results):
        """Test simulated ping flows with mock echo/ACK responses"""
        if has_console:
            print("\n🔄 Testing Simulated Ping Flows:")
        
        # === Test 1: Successful Single Ping ===
        try:
            # Start a ping test
            test_start_time = time.time()
            
            # Simulate echo message
            echo_data = {
                'src': self.my_callsign,
                'dst': 'W1ABC-1', 
                'msg': 'Ping test 1/1 to measure roundtrip{123'
            }
            
            await self._handle_echo_message(echo_data)
            
            # Check if ping is tracked
            ping_tracked = '123' in self.active_pings
            status = "✅ PASS" if ping_tracked else "❌ FAIL"
            results.append((status, "Echo tracking", ping_tracked))
            
            if has_console:
                print(f"{status} | Echo tracking")
            
            # Wait a moment
            await asyncio.sleep(0.1)
            
            # Simulate ACK response
            ack_data = {
                'src': 'W1ABC-1',
                'dst': self.my_callsign,
                'msg': f'{self.my_callsign}  :ack123'
            }
            
            await self._handle_ack_message(ack_data)
            
            # Check if ping completed (removed from active)
            ping_completed = '123' not in self.active_pings
            status = "✅ PASS" if ping_completed else "❌ FAIL"
            results.append((status, "ACK processing and cleanup", ping_completed))
            
            if has_console:
                print(f"{status} | ACK processing and cleanup")
            
        except Exception as e:
            status = "❌ ERROR"
            results.append((status, "Simulated ping flow", False))
            if has_console:
                print(f"{status} | Simulated ping flow - Exception: {e}")
        
        # === Test 2: Timeout Scenario ===
        try:
            # Simulate echo without ACK to test timeout
            echo_data = {
                'src': self.my_callsign,
                'dst': 'TIMEOUT-NODE',
                'msg': 'Ping test 1/1 to measure roundtrip{456'
            }
            
            await self._handle_echo_message(echo_data)
            
            # Check immediate tracking
            timeout_tracked = '456' in self.active_pings
            status = "✅ PASS" if timeout_tracked else "❌ FAIL"
            results.append((status, "Timeout scenario setup", timeout_tracked))
            
            if has_console:
                print(f"{status} | Timeout scenario setup")
            
            # Note: Full timeout test would take 30 seconds, so we just verify setup
            
        except Exception as e:
            status = "❌ ERROR"
            results.append((status, "Timeout scenario", False))
            if has_console:
                print(f"{status} | Timeout scenario - Exception: {e}")
        
        # === Test 3: Invalid ACK Scenarios ===
        invalid_ack_tests = [
            # (ack_data, should_be_ignored, description)
            ({'src': 'WRONG-NODE', 'dst': self.my_callsign, 'msg': f'{self.my_callsign} :ack456'}, True, "ACK from wrong sender"),
            ({'src': 'TIMEOUT-NODE', 'dst': 'WRONG-DST', 'msg': 'WRONG-DST :ack456'}, True, "ACK to wrong destination"),
            ({'src': 'TIMEOUT-NODE', 'dst': self.my_callsign, 'msg': f'{self.my_callsign} :ack999'}, True, "ACK with unknown ID"),
        ]
        
        for ack_data, should_ignore, description in invalid_ack_tests:
            try:
                # Store state before
                pings_before = len(self.active_pings)
                
                await self._handle_ack_message(ack_data)
                
                # Check if ping count unchanged (ACK ignored)
                pings_after = len(self.active_pings)
                ack_ignored = (pings_before == pings_after) == should_ignore
                
                status = "✅ PASS" if ack_ignored else "❌ FAIL"
                results.append((status, description, ack_ignored))
                
                if has_console:
                    print(f"{status} | {description}")
                    
            except Exception as e:
                status = "❌ ERROR"
                results.append((status, description, False))
                if has_console:
                    print(f"{status} | {description} - Exception: {e}")
    
    async def _cleanup_test_ctcping(self):
        """Clean up test data for CTC ping tests"""
        self.active_pings.clear()
        if hasattr(self, 'ping_tests'):
            self.ping_tests.clear()























    async def _monitor_test_completion(self, test_id: str):
        """Monitor test completion and send summary when done"""
        try:
            # Wait for test to complete or timeout (max 5 minutes total)
            start_time = time.time()
            max_wait = 300  # 5 minutes
            
            while (time.time() - start_time) < max_wait:
                if test_id not in self.ping_tests:
                    return  # Test was cancelled or removed
                
                test_summary = self.ping_tests[test_id]
                
                # Check if all pings completed (success + timeout)
                total_completed = test_summary['completed'] + test_summary['timeouts']
                
                if total_completed >= test_summary['total_pings']:
                    # Test completed
                    test_summary['status'] = 'completed'
                    test_summary['end_time'] = time.time()
                    await self._send_test_summary(test_id)
                    return
                
                # Wait and check again
                await asyncio.sleep(1.0)
            
            # Test timeout
            if test_id in self.ping_tests:
                test_summary = self.ping_tests[test_id]
                test_summary['status'] = 'timeout'
                test_summary['end_time'] = time.time()
                await self._send_test_summary(test_id, "Test timeout after 5 minutes")
                
        except Exception as e:
            if has_console:
                print(f"❌ Error monitoring test completion: {e}")






    async def _send_test_summary(self, test_id: str, error_msg: str = None):
        """Send complete test summary to requester"""
        try:
            if test_id not in self.ping_tests:
                return
            
            test_summary = self.ping_tests[test_id]
            
            if error_msg:
                # Send error message
                await self._send_ping_result(test_summary['requester'], f"🏓 {error_msg}")
            else:
                # Calculate statistics
                results = test_summary['results']
                total_pings = test_summary['total_pings']
                successful = len(results)
                timeouts = test_summary['timeouts']
                loss_percent = int(((total_pings - successful) / total_pings) * 100)
                
                target = test_summary['target']
                payload_size = test_summary['payload_size']
                
                if successful > 0:
                    rtts = [r['rtt'] for r in results]
                    min_rtt = min(rtts) * 1000
                    max_rtt = max(rtts) * 1000
                    avg_rtt = (sum(rtts) / len(rtts)) * 1000
                    
                    summary_msg = f"🏓 Ping summary to {target}: {successful}/{total_pings} replies, {loss_percent}% loss, {payload_size}B payload. RTT min/avg/max = {min_rtt:.1f}/{avg_rtt:.1f}/{max_rtt:.1f}ms"
                else:
                    summary_msg = f"🏓 Ping summary to {target}: {loss_percent}% packet loss ({successful}/{total_pings}), {payload_size}B payload"
                
                await self._send_ping_result(test_summary['requester'], summary_msg)
            
            # Cleanup test
            del self.ping_tests[test_id]
            
            if has_console:
                print(f"📊 Test summary sent for {test_id}")
                
        except Exception as e:
            if has_console:
                print(f"❌ Error sending test summary: {e}")




    
    async def _send_ping_result(self, requester: str, result_message: str):
        """Send ping result to requester"""
        try:
            if self.message_router:
                result_data = {
                    'dst': requester,
                    'msg': result_message,
                    'src_type': 'ctcping_result',
                    'type': 'msg'
                }
                
                # Route based on requester
                if requester == self.my_callsign:
                    await self.message_router.publish('ctcping', 'websocket_message', result_data)
                else:
                    await self.message_router.publish('ctcping', 'udp_message', result_data)
                    
        except Exception as e:
            if has_console:
                print(f"❌ Failed to send ping result: {e}")








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

    async def handle_userinfo(self, kwargs, requester):
        """Show user information from config"""
        try:
            # Get config through message router (needs access to config)
            user_info = getattr(self, 'user_info_text', None)
            
            if not user_info:
                return "❌ User info not configured"
                
            return f"ℹ️ {user_info}"
            
        except Exception as e:
            return f"❌ Error retrieving user info: {str(e)[:30]}"

    async def handle_kickban(self, kwargs, requester):
        """Manage blocked callsigns"""
        if not self._is_admin(requester):
            return "❌ Admin access required"
        
        # !kb oder !kb list
        if not kwargs or kwargs.get('callsign') == 'list':
            if not self.blocked_callsigns:
                return "📋 Blocklist is empty"
            blocked_list = ', '.join(sorted(self.blocked_callsigns))
            return f"🚫 Blocked: {blocked_list}"
        
        # !kb delall
        if kwargs.get('callsign') == 'delall':
            count = len(self.blocked_callsigns)
            self.blocked_callsigns.clear()
            return f"✅ Cleared {count} blocked callsign(s)"
        
        callsign = kwargs.get('callsign', '').upper()
        action = kwargs.get('action', '').lower()
        
        # Validate callsign
        if not re.match(r'^[A-Z]{1,2}[0-9][A-Z]{1,3}(-\d{1,2})?$', callsign):
            return "❌ Invalid callsign format"
        
        # Prevent self-blocking
        if callsign.split('-')[0] == self.admin_callsign_base:
            return "❌ Cannot block own callsign"
        
        # !kb CALL del
        if action == 'del':
            if callsign in self.blocked_callsigns:
                self.blocked_callsigns.remove(callsign)
                return f"✅ {callsign} unblocked"
            else:
                return f"ℹ️ {callsign} was not blocked"
        
        # !kb CALL (add to blocklist)
        if callsign in self.blocked_callsigns:
            return f"ℹ️ {callsign} already blocked"
        
        self.blocked_callsigns.add(callsign)
        return f"🚫 {callsign} blocked"


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






    async def handle_topic(self, kwargs, requester):
        """Manage group beacon messages"""
        if not self._is_admin(requester):
            return "❌ Admin access required"
        
        # !topic (show all active topics)
        if not kwargs:
            if not self.active_topics:
                return "📡 No active beacon topics"
            
            topics_info = []
            for group, info in self.active_topics.items():
                interval = info['interval']
                text_preview = info['text'][:30] + ('...' if len(info['text']) > 30 else '')
                topics_info.append(f"Group {group}: '{text_preview}' every {interval}min")
            
            return f"📡 Active beacons: {' | '.join(topics_info)}"
        
        # !topic delete GROUP
        if kwargs.get('action') == 'delete':
            group = kwargs.get('group')
            if not group:
                return "❌ Group required for delete"
            
            if not self.is_group(group):
                return "❌ Invalid group format"
            
            if group not in self.active_topics:
                return f"ℹ️ No beacon active for group {group}"
            
            # Stop and remove the beacon
            await self._stop_topic_beacon(group)
            return f"✅ Beacon stopped for group {group}"
        
        # !topic GROUP TEXT [interval]
        group = kwargs.get('group')
        text = kwargs.get('text', '')
        interval = kwargs.get('interval', 30)  # Default 30 minutes (29:50 after 10s deduction)
        
        if not group:
            return "❌ Group required"
        
        if not self.is_group(group):
            return "❌ Invalid group format (use digits 1-99999 or TEST)"
        
        if not text:
            return "❌ Beacon text required"
        
        if len(text) > 200:
            return "❌ Beacon text too long (max 200 chars)"
        
        try:
            interval = int(interval)
            if interval < 1 or interval > 1440:  # 1 minute to 24 hours
                return "❌ Interval must be between 1 and 1440 minutes"
        except (ValueError, TypeError):
            return "❌ Invalid interval format"
        
        # Stop existing beacon for this group if any
        if group in self.active_topics:
            await self._stop_topic_beacon(group)
        
        # Start new beacon
        success = await self._start_topic_beacon(group, text, interval)
        
        if success:
            return f"✅ Beacon started for group {group}: '{text[:50]}{'...' if len(text) > 50 else ''}' every {interval}min"
        else:
            return "❌ Failed to start beacon"
    
    # Beacon management methods:
    async def _start_topic_beacon(self, group, text, interval_minutes):
        """Start a beacon task for a group"""
        try:
            # Convert to seconds and subtract 10 seconds as specified
            interval_seconds = (interval_minutes * 60) - 10
            if interval_seconds < 10:  # Minimum 10 seconds
                interval_seconds = 10
            
            # Create and start the beacon task
            task = asyncio.create_task(self._beacon_loop(group, text, interval_seconds))
            
            # Store beacon info
            self.active_topics[group] = {
                'text': text,
                'interval': interval_minutes,
                'task': task,
                'started': datetime.now()
            }
            
            # Track task for cleanup
            self.topic_tasks.add(task)
            
            # Remove from tracking when done
            task.add_done_callback(self.topic_tasks.discard)
            
            if has_console:
                print(f"📡 Started beacon for group {group}: interval {interval_seconds}s")
            
            return True
            
        except Exception as e:
            if has_console:
                print(f"❌ Failed to start beacon for group {group}: {e}")
            return False
    
    async def _stop_topic_beacon(self, group):
        """Stop a beacon task for a group"""
        if group not in self.active_topics:
            return False
        
        try:
            topic_info = self.active_topics[group]
            task = topic_info['task']
            
            # Cancel the task
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass  # Expected when cancelling
            
            # Remove from active topics
            del self.active_topics[group]
            
            if has_console:
                print(f"📡 Stopped beacon for group {group}")
            
            return True
            
        except Exception as e:
            if has_console:
                print(f"❌ Failed to stop beacon for group {group}: {e}")
            return False
    
    async def _beacon_loop(self, group, text, interval_seconds):
        """Beacon loop - sends periodic messages to a group"""
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                
                # Check if we're still supposed to be running
                if group not in self.active_topics:
                    break
                
                # Send beacon message
                await self._send_beacon_message(group, text)
                
                if has_console:
                    print(f"📡 Sent beacon to group {group}: '{text[:30]}...'")
        
        except asyncio.CancelledError:
            if has_console:
                print(f"📡 Beacon loop cancelled for group {group}")
            raise  # Re-raise to properly handle cancellation
        
        except Exception as e:
            if has_console:
                print(f"❌ Beacon loop error for group {group}: {e}")
            
            # Remove from active topics on error
            if group in self.active_topics:
                del self.active_topics[group]
    
    async def _send_beacon_message(self, group, text):
        """Send a beacon message to a group"""
        try:
            if self.message_router:
                beacon_message = {
                    'dst': group,
                    'msg': f"📡 {text}",
                    'src_type': 'beacon',
                    'type': 'msg'
                }
                
                # Send via UDP (to mesh network)
                await self.message_router.publish('beacon', 'udp_message', beacon_message)
                
        except Exception as e:
            if has_console:
                print(f"❌ Failed to send beacon message to group {group}: {e}")
    
    # Cleanup method for shutdown:
    async def cleanup_topic_beacons(self):
        """Clean up all running beacon tasks"""
        if has_console:
            print(f"🧹 Cleaning up {len(self.active_topics)} beacon tasks...")
        
        # Stop all beacons
        groups_to_stop = list(self.active_topics.keys())
        for group in groups_to_stop:
            await self._stop_topic_beacon(group)
        
        # Cancel any remaining tasks
        remaining_tasks = [task for task in self.topic_tasks if not task.done()]
        if remaining_tasks:
            for task in remaining_tasks:
                task.cancel()
            
            # Wait for all to complete
            try:
                await asyncio.gather(*remaining_tasks, return_exceptions=True)
            except Exception:
                pass  # Ignore exceptions during cleanup
        
        self.topic_tasks.clear()
        
        if has_console:
            print("✅ All beacon tasks cleaned up")





    def test_reception_logic(self):
        """Test reception logic based on the table scenarios"""
        if has_console:
            print("\n🧪 Testing Reception Logic:")
            print("=" * 50)
        
        test_cases = [
            # (src, dst, msg, groups_enabled, expected_execution, expected_type, description)
            
            # === Leeres/ungültiges Ziel ===
            ("OE1ABC-5", "", "!WX", True, False, None, "Leeres Ziel → keine Ausführung"),
            ("OE1ABC-5", "*", "!WX", True, False, None, "Ungültiges Ziel (*) → keine Ausführung"),
            ("OE1ABC-5", "ALL", "!WX", True, False, None, "Ungültiges Ziel (ALL) → keine Ausführung"),
            
            # === Gruppe ohne my_callsign Target ===
            (self.admin_callsign_base, "20", "!WX", True, False, None, "Gruppe ohne Target (Admin) → keine Ausführung"),
            (self.admin_callsign_base, "20", "!WX", False, False, None, "Gruppe ohne Target (Admin, Groups OFF) → keine Ausführung"),
            ("OE1ABC-5", "20", "!STATS", True, False, None, "Gruppe ohne Target (User, Groups ON) → keine Ausführung"),
            ("OE1ABC-5", "20", "!STATS", False, False, None, "Gruppe ohne Target (User, Groups OFF) → keine Ausführung"),
            
            # === Gruppe mit my_callsign Target ===
            (self.admin_callsign_base, "20", f"!WX {self.my_callsign}", True, True, 'group', "Gruppe mit Target (Admin, Groups ON) → Ausführung"),
            (self.admin_callsign_base, "20", f"!WX {self.my_callsign}", False, True, 'group', "Gruppe mit Target (Admin, Groups OFF) → Admin override"),
            ("OE1ABC-5", "20", f"!TIME {self.my_callsign}", True, True, 'group', "Gruppe mit Target (User, Groups ON) → Ausführung"),
            ("OE1ABC-5", "20", f"!TIME {self.my_callsign}", False, False, None, "Gruppe mit Target (User, Groups OFF) → keine Ausführung"),
            
            # === Test-Gruppe ===
            (self.admin_callsign_base, "TEST", f"!WX {self.my_callsign}", True, True, 'group', "Test-Gruppe (Admin) → Ausführung"),
            ("OE1ABC-5", "TEST", f"!TIME {self.my_callsign}", False, False, None, "Test-Gruppe (User, Groups OFF) → keine Ausführung"),
            
            # === Direkt ohne Target ===
            (self.admin_callsign_base, self.my_callsign, "!TIME", True, False, None, "Direkt ohne Target (Admin) → keine Ausführung"),
            ("OE1ABC-5", self.my_callsign, "!DICE", True, False, None, "Direkt ohne Target (User) → keine Ausführung"),
            
            # === Direkt mit my_callsign Target ===
            (self.admin_callsign_base, self.my_callsign, f"!TIME {self.my_callsign}", True, True, 'direct', "Direkt mit Target (Admin) → Ausführung"),
            ("OE1ABC-5", self.my_callsign, f"!DICE {self.my_callsign}", True, True, 'direct', "Direkt mit Target (User) → Ausführung"),
            ("OE1ABC-5", self.my_callsign, f"!DICE {self.my_callsign}", False, True, 'direct', "Direkt mit Target (User, Groups OFF) → Ausführung"),
            
            # === Direkt an anderen ===
            (self.admin_callsign_base, "OE1ABC-5", "!WX", True, False, None, "Direkt an anderen → keine Ausführung"),
            
            # === Edge Cases ===
            ("OE1ABC-5", "20", "!WX OE1ABC-5", True, False, None, "Gruppe mit fremdem Target → keine Ausführung"),
            (self.my_callsign, "20", f"!WX {self.my_callsign}", True, True, 'group', "Eigene Nachricht mit Target → Ausführung"),
        ]
        
        results = []
        for src, dst, msg, groups_enabled, expected_exec, expected_type, description in test_cases:
            # Setup test environment
            old_groups_setting = self.group_responses_enabled
            self.group_responses_enabled = groups_enabled
            
            try:
                # Test the logic
                actual_exec, actual_type = self._should_execute_command(src, dst, msg)
                
                # Check results
                exec_match = actual_exec == expected_exec
                type_match = actual_type == expected_type
                overall_pass = exec_match and type_match
                
                status = "✅ PASS" if overall_pass else "❌ FAIL"
                
                results.append((status, description, actual_exec, expected_exec, actual_type, expected_type))
                
                if has_console:
                    print(f"{status} | {description}")
                    print(f"     {src}→{dst} '{msg[:30]}...'")
                    print(f"     Groups: {'ON' if groups_enabled else 'OFF'} | Execute: {actual_exec} (exp: {expected_exec}) | Type: {actual_type} (exp: {expected_type})")
                    if not overall_pass:
                        if not exec_match:
                            print(f"     ❌ Execution mismatch: got {actual_exec}, expected {expected_exec}")
                        if not type_match:
                            print(f"     ❌ Type mismatch: got {actual_type}, expected {expected_type}")
                    print()
                    
            finally:
                # Restore original setting
                self.group_responses_enabled = old_groups_setting
        
        # Summary
        passed = sum(1 for r in results if r[0].startswith("✅"))
        total = len(results)
        
        if has_console:
            print(f"🧪 Reception Test Summary: {passed}/{total} tests passed")
            if passed == total:
                print("🎉 All reception tests passed!")
            else:
                print("⚠️ Some reception tests failed - check logic!")
                
                # Show failed tests
                failed_tests = [r for r in results if r[0].startswith("❌")]
                if failed_tests:
                    print("\n❌ Failed Tests:")
                    for status, description, actual_exec, expected_exec, actual_type, expected_type in failed_tests:
                        print(f"   • {description}")
                        print(f"     Expected: execute={expected_exec}, type={expected_type}")
                        print(f"     Actual:   execute={actual_exec}, type={actual_type}")
            
            print("=" * 50)
        
        return passed == total



    async def test_reception_edge_cases(self):
        """Test edge cases and boundary conditions"""
        if has_console:
            print("\n🧪 Testing Reception Edge Cases:")
            print("=" * 30)
        
        edge_cases = [
            # (src, dst, msg, groups_enabled, expected_execution, expected_type, description)
            
            # === Case sensitivity ===
            ("oe1abc-5", self.my_callsign.lower(), f"!time {self.my_callsign.lower()}", True, True, 'direct', "Lowercase handling"),
            
            # === Mixed case targets ===
            ("OE1ABC-5", "20", f"!wx {self.my_callsign.lower()}", True, True, 'group', "Mixed case target"),
            
            # === Complex callsigns ===
            ("EA1ABC-15", "TEST", f"!stats {self.my_callsign}", True, True, 'group', "Complex callsign (EA prefix)"),
            
            # === Short callsigns ===
            ("W1A-1", "50", f"!time {self.my_callsign}", True, True, 'group', "Short callsign (W1A)"),
            
            # === Admin with SID ===
            (f"{self.admin_callsign_base}-99", "20", f"!wx {self.my_callsign}", False, True, 'group', "Admin with high SID"),
            
            # === Multiple targets (should use last one) ===
            ("OE1ABC-5", "20", f"!wx OE1ABC-5 {self.my_callsign}", True, True, 'group', "Multiple targets (last one wins)"),
            
            # === Very long callsigns (edge case) ===
            ("VK9ABCD-12", "TEST", f"!time {self.my_callsign}", True, True, 'group', "Long callsign"),
        ]
        
        results = []
        for src, dst, msg, groups_enabled, expected_exec, expected_type, description in edge_cases:
            old_groups_setting = self.group_responses_enabled
            self.group_responses_enabled = groups_enabled
            
            try:
                actual_exec, actual_type = self._should_execute_command(src, dst, msg)
                
                exec_match = actual_exec == expected_exec
                type_match = actual_type == expected_type
                overall_pass = exec_match and type_match
                
                status = "✅ PASS" if overall_pass else "❌ FAIL"
                results.append((status, description, overall_pass))
                
                if has_console:
                    print(f"{status} | {description}")
                    if not overall_pass:
                        print(f"     Expected: execute={expected_exec}, type={expected_type}")
                        print(f"     Actual:   execute={actual_exec}, type={actual_type}")
                    
            finally:
                self.group_responses_enabled = old_groups_setting
        
        passed = sum(1 for r in results if r[2])
        total = len(results)
        
        if has_console:
            print(f"🧪 Edge Case Summary: {passed}/{total} tests passed")
            print("=" * 30)
        
        return passed == total
    
    async def test_kickban_logic(self):
        """Test kick-ban functionality"""
        if has_console:
            print("\n🧪 Testing Kick-Ban Logic:")
            print("=" * 40)
        
        test_cases = [
            # (requester, args, initial_blocked, expected_result_contains, expected_blocked_after, description)
            
            # === Admin Tests ===
            (self.admin_callsign_base, {}, set(), "Blocklist is empty", set(), "Empty list display"),
            (self.admin_callsign_base, {'callsign': 'list'}, set(), "Blocklist is empty", set(), "Explicit list command"),
            
            # === Add to blocklist ===
            (self.admin_callsign_base, {'callsign': 'OE1ABC-5'}, set(), "🚫 OE1ABC-5 blocked", {'OE1ABC-5'}, "Add callsign to blocklist"),
            (self.admin_callsign_base, {'callsign': 'OE1ABC-5'}, {'OE1ABC-5'}, "already blocked", {'OE1ABC-5'}, "Add already blocked callsign"),
            
            # === Remove from blocklist ===
            (self.admin_callsign_base, {'callsign': 'OE1ABC-5', 'action': 'del'}, {'OE1ABC-5'}, "✅ OE1ABC-5 unblocked", set(), "Remove from blocklist"),
            (self.admin_callsign_base, {'callsign': 'OE1ABC-5', 'action': 'del'}, set(), "was not blocked", set(), "Remove non-blocked callsign"),
            
            # === List with content ===
            (self.admin_callsign_base, {}, {'OE1ABC-5', 'W1XYZ-1'}, "🚫 Blocked: OE1ABC-5, W1XYZ-1", {'OE1ABC-5', 'W1XYZ-1'}, "List multiple blocked"),
            
            # === Clear all ===
            (self.admin_callsign_base, {'callsign': 'delall'}, {'OE1ABC-5', 'W1XYZ-1'}, "✅ Cleared 2 blocked", set(), "Clear all blocked"),
            (self.admin_callsign_base, {'callsign': 'delall'}, set(), "✅ Cleared 0 blocked", set(), "Clear empty list"),
            
            # === Self-blocking prevention ===
            (self.admin_callsign_base, {'callsign': self.my_callsign}, set(), "❌ Cannot block own callsign", set(), "Prevent self-blocking (exact)"),
            (self.admin_callsign_base, {'callsign': f'{self.admin_callsign_base}-99'}, set(), "❌ Cannot block own callsign", set(), "Prevent self-blocking (base)"),
            
            # === Invalid callsigns ===
            (self.admin_callsign_base, {'callsign': 'INVALID'}, set(), "❌ Invalid callsign format", set(), "Invalid callsign format"),
            (self.admin_callsign_base, {'callsign': 'TOO-LONG-123'}, set(), "❌ Invalid callsign format", set(), "Invalid callsign (too long)"),
            
            # === Non-admin tests ===
            ("OE1ABC-5", {}, set(), "❌ Admin access required", set(), "Non-admin list attempt"),
            ("OE1ABC-5", {'callsign': 'W1XYZ-1'}, set(), "❌ Admin access required", set(), "Non-admin block attempt"),
            ("OE1ABC-5", {'callsign': 'delall'}, {'OE1ABC-5'}, "❌ Admin access required", {'OE1ABC-5'}, "Non-admin clear attempt"),
        ]
        
        results = []
        for requester, args, initial_blocked, expected_contains, expected_blocked_after, description in test_cases:
            # Setup test environment
            old_blocked = self.blocked_callsigns.copy()
            self.blocked_callsigns = initial_blocked.copy()
            
            try:
                # Execute command
                result = await self.handle_kickban(args, requester)
                
                # Check result contains expected text
                result_match = expected_contains.lower() in result.lower()
                
                # Check final state
                state_match = self.blocked_callsigns == expected_blocked_after
                
                overall_pass = result_match and state_match
                status = "✅ PASS" if overall_pass else "❌ FAIL"
                
                results.append((status, description, overall_pass))
                
                if has_console:
                    print(f"{status} | {description}")
                    print(f"     Requester: {requester}")
                    print(f"     Args: {args}")
                    print(f"     Result: '{result}'")
                    if not result_match:
                        print(f"     ❌ Result should contain: '{expected_contains}'")
                    if not state_match:
                        print(f"     ❌ Expected blocked: {expected_blocked_after}")
                        print(f"     ❌ Actual blocked: {self.blocked_callsigns}")
                    print()
                    
            except Exception as e:
                status = "❌ ERROR"
                results.append((status, description, False))
                if has_console:
                    print(f"{status} | {description}")
                    print(f"     Exception: {e}")
                    print()
                    
            finally:
                # Restore original state
                self.blocked_callsigns = old_blocked
        
        # Summary
        passed = sum(1 for r in results if r[2])
        total = len(results)
        
        if has_console:
            print(f"🧪 Kick-Ban Test Summary: {passed}/{total} tests passed")
            if passed == total:
                print("🎉 All kick-ban tests passed!")
            else:
                print("⚠️ Some kick-ban tests failed!")
                
                # Show failed tests
                failed_tests = [r for r in results if not r[2]]
                if failed_tests:
                    print("\n❌ Failed Tests:")
                    for status, description, _ in failed_tests:
                        print(f"   • {description}")
            
            print("=" * 40)
        
        return passed == total
    
    def test_message_blocking_integration(self):
        """Test message blocking integration logic"""
        if has_console:
            print("\n🧪 Testing Message Blocking Integration:")
            print("=" * 45)
        
        test_callsigns = [
            ("OE1ABC-5", False, "Blocked callsign should be filtered"),
            ("W1XYZ-1", True, "Non-blocked callsign should pass"),  
            ("DK5EN-1", True, "Own callsign should always pass"),
            ("oe1abc-5", False, "Blocked callsign (lowercase) should be filtered"),
        ]
        
        results = []
        
        # Setup: Block OE1ABC-5
        old_blocked = getattr(self, 'blocked_callsigns', set())
        self.blocked_callsigns = {"OE1ABC-5"}
        
        try:
            for callsign, should_pass, description in test_callsigns:
                # Test the blocking logic
                callsign_upper = callsign.upper()
                is_blocked = callsign_upper in self.blocked_callsigns
                result_correct = (not is_blocked) == should_pass
                
                status = "✅ PASS" if result_correct else "❌ FAIL"
                results.append((status, description, result_correct))
                
                if has_console:
                    print(f"{status} | {description}")
                    print(f"     Callsign: {callsign} -> {callsign_upper}, Blocked: {is_blocked}, Should pass: {should_pass}")
            
            # Test edge cases
            edge_cases = [
                ("", False, "Empty callsign should be blocked"),
                ("INVALID_FORMAT", True, "Invalid format should pass (handled elsewhere)"),
            ]
            
            for callsign, should_pass, description in edge_cases:
                callsign_upper = callsign.upper()
                is_blocked = callsign_upper in self.blocked_callsigns if callsign_upper else True
                result_correct = (not is_blocked) == should_pass
                
                status = "✅ PASS" if result_correct else "❌ FAIL"
                results.append((status, description, result_correct))
                
                if has_console:
                    print(f"{status} | {description}")
                    print(f"     Callsign: '{callsign}' -> '{callsign_upper}', Blocked: {is_blocked}, Should pass: {should_pass}")
            
        finally:
            # Restore original state
            self.blocked_callsigns = old_blocked
        
        passed = sum(1 for r in results if r[2])
        total = len(results)
        
        if has_console:
            print(f"🧪 Blocking Integration Summary: {passed}/{total} tests passed")
            print("=" * 45)
        
        return passed == total
    
    async def run_all_tests(self):
        """Run complete test suite for CommandHandler"""
        if has_console:
            print("\n" + "="*60)
            print("🧪 COMMAND HANDLER TEST SUITE")
            print("="*60)
        
        basic_passed = self.test_reception_logic()
        edge_passed = await self.test_reception_edge_cases()
        kickban_passed = await self.test_kickban_logic()
        blocking_passed = self.test_message_blocking_integration()
        topic_passed = await self.test_topic_logic()
        ctcping_passed = await self.test_ctcping_logic()  # NEUE ZEILE
    
        total_passed = all([basic_passed, edge_passed, kickban_passed, blocking_passed, topic_passed, ctcping_passed])
    
        
        if has_console:
            if total_passed:
                print("\n🎉 ALL COMMAND HANDLER TESTS PASSED!")
            else:
                print("\n⚠️ SOME COMMAND HANDLER TESTS FAILED!")
            print("="*60)
        
        return total_passed






    async def test_topic_logic(self):
        """Test topic/beacon functionality"""
        if has_console:
            print("\n🧪 Testing Topic Logic:")
            print("=" * 35)
        
        test_cases = [
            # (requester, args, expected_result_contains, description)
            
            # === Admin permission tests ===
            ("OE1ABC-5", {}, "❌ Admin access required", "Non-admin access denied"),
            
            # === Empty list ===
            (self.admin_callsign_base, {}, "📡 No active beacon topics", "Empty topic list"),
            
            # === Invalid group formats ===
            (self.admin_callsign_base, {'group': 'INVALID'}, "❌ Invalid group format", "Invalid group name"),
            (self.admin_callsign_base, {'group': '123456'}, "❌ Invalid group format", "Group number too long"),
            
            # === Missing parameters ===
            (self.admin_callsign_base, {'group': '20'}, "❌ Beacon text required", "Missing beacon text"),
            (self.admin_callsign_base, {'text': 'Hello World'}, "❌ Group required", "Missing group"),
            
            # === Text length validation ===
            (self.admin_callsign_base, {'group': '20', 'text': 'x' * 201}, "❌ Beacon text too long", "Text too long"),
            
            # === Interval validation ===
            (self.admin_callsign_base, {'group': '20', 'text': 'Test', 'interval': 0}, "❌ Interval must be between", "Interval too small"),
            (self.admin_callsign_base, {'group': '20', 'text': 'Test', 'interval': 1441}, "❌ Interval must be between", "Interval too large"),
            (self.admin_callsign_base, {'group': '20', 'text': 'Test', 'interval': 'invalid'}, "❌ Invalid interval format", "Invalid interval format"),
            
            # === Valid beacon creation ===
            (self.admin_callsign_base, {'group': '20', 'text': 'Test beacon', 'interval': 30}, "✅ Beacon started", "Valid beacon creation"),
            (self.admin_callsign_base, {'group': 'TEST', 'text': 'Another beacon'}, "✅ Beacon started", "Valid beacon with default interval"),
            
            # === Delete operations ===
            (self.admin_callsign_base, {'action': 'delete', 'group': '999'}, "ℹ️ No beacon active", "Delete non-existent beacon"),
            (self.admin_callsign_base, {'action': 'delete', 'group': '20'}, "✅ Beacon stopped", "Delete existing beacon"),
            (self.admin_callsign_base, {'action': 'delete'}, "❌ Group required", "Delete without group"),
            
            # === List with active beacons ===
            # (Will be tested after setting up some beacons)
        ]
        
        results = []
        
        # Ensure clean start
        await self._cleanup_test_beacons()
        
        for requester, args, expected_contains, description in test_cases:
            try:
                result = await self.handle_topic(args, requester)
                
                result_match = expected_contains.lower() in result.lower()
                status = "✅ PASS" if result_match else "❌ FAIL"
                
                results.append((status, description, result_match))
                
                if has_console:
                    print(f"{status} | {description}")
                    print(f"     Args: {args}")
                    print(f"     Result: '{result}'")
                    if not result_match:
                        print(f"     ❌ Should contain: '{expected_contains}'")
                    print()
                    
            except Exception as e:
                status = "❌ ERROR"
                results.append((status, description, False))
                if has_console:
                    print(f"{status} | {description}")
                    print(f"     Exception: {e}")
                    print()
        
        # Test beacon listing with active beacons
        try:
            # Set up test beacons
            await self.handle_topic({'group': '50', 'text': 'Test beacon 1', 'interval': 60}, self.admin_callsign_base)
            await self.handle_topic({'group': '51', 'text': 'Test beacon 2', 'interval': 120}, self.admin_callsign_base)
            
            # Test listing
            list_result = await self.handle_topic({}, self.admin_callsign_base)
            list_contains_50 = "Group 50" in list_result
            list_contains_51 = "Group 51" in list_result
            list_success = list_contains_50 and list_contains_51
            
            status = "✅ PASS" if list_success else "❌ FAIL"
            results.append((status, "List active beacons", list_success))
            
            if has_console:
                print(f"{status} | List active beacons")
                print(f"     Result: '{list_result}'")
                if not list_success:
                    print(f"     ❌ Should contain both Group 50 and Group 51")
                print()
            
        except Exception as e:
            status = "❌ ERROR"
            results.append((status, "List active beacons", False))
            if has_console:
                print(f"{status} | List active beacons")
                print(f"     Exception: {e}")
                print()
        
        # Cleanup test beacons
        await self._cleanup_test_beacons()
        
        # Summary
        passed = sum(1 for r in results if r[2])
        total = len(results)
        
        if has_console:
            print(f"🧪 Topic Test Summary: {passed}/{total} tests passed")
            if passed == total:
                print("🎉 All topic tests passed!")
            else:
                print("⚠️ Some topic tests failed!")
                
                failed_tests = [r for r in results if not r[2]]
                if failed_tests:
                    print("\n❌ Failed Tests:")
                    for status, description, _ in failed_tests:
                        print(f"   • {description}")
            
            print("=" * 35)
        
        return passed == total
    
    async def _cleanup_test_beacons(self):
        """Clean up any test beacons"""
        test_groups = ['50', '51', '52', '99', 'TEST']
        for group in test_groups:
            if group in self.active_topics:
                await self._stop_topic_beacon(group)







# Integration function for your main script
def create_command_handler(message_router, storage_handler, call_sign, lat, long, stat_name, user_info_text):
    """Factory function to create and integrate CommandHandler"""
    return CommandHandler(message_router, storage_handler, call_sign, lat, long, stat_name, user_info_text)
