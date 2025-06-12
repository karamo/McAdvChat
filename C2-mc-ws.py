#!/usr/bin/env python3
from message_storage import MessageStorageHandler
from udp_handler import UDPHandler
from websocket_handler import WebSocketManager

from ble_handler import (
    ble_connect, ble_disconnect, ble_pair, ble_unpair,
    scan_ble_devices, backend_resolve_ip, get_ble_client,
    handle_a0_command, handle_set_command, handle_ble_message
)

from command_handler import create_command_handler

VERSION="v0.50.0"

#### debug
import signal
import traceback

def debug_signal_handler(signum, frame):
    """Print stack trace when USR1 signal received"""
    print("=" * 60)
    print("🔍 DEBUG: Stack trace at hang point:")
    print("=" * 60)
    traceback.print_stack(frame)
    print("=" * 60)
#### debug


import asyncio
import errno
import json
import os
import re
import signal
import socket
import sys
import unicodedata
import websockets
from struct import *

import time

from collections import deque, defaultdict


CONFIG_FILE = "/etc/mcadvchat/config.json"
if os.getenv("MCADVCHAT_ENV") == "dev":
   print("*** Debug 🐛 and 🔧 DEV Environment detected ***")
   CONFIG_FILE = "/etc/mcadvchat/config.dev.json"

block_list = [
  "response",
  "OE0XXX-99",
]


def load_config(path=CONFIG_FILE):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg

def hours_to_dd_hhmm(hours: int) -> str:
    days = hours // 24
    remainder_hours = hours % 24
    return f"{days:02d} day(s) {remainder_hours:02d}:00h"

class MessageRouter:
    def __init__(self, message_storage_handler=None):
        self._subscribers = defaultdict(list)
        self._protocols = {}
        self.storage_handler = message_storage_handler
        self.logger = print
        self.my_callsign = None
        self.validator = None

        if message_storage_handler:
            self.subscribe('mesh_message', self._storage_handler)
            self.subscribe('ble_notification', self._storage_handler)

        self.subscribe('ble_message', self._ble_message_handler)  
        self.subscribe('udp_message', self._udp_message_handler) 

    def set_callsign(self, callsign):
        """Set the callsign from config"""
        self.my_callsign = callsign.upper()
        self.validator = MessageValidator(self.my_callsign)
        if has_console:
            print(f"🔧 MessageRouter: Callsign set to '{self.my_callsign}', validator initialized")


    def test_suppression_logic(self):
        """Test suppression logic based on the table scenarios"""
        if has_console:
            print("\n🧪 Testing Suppression Logic:")
            print("=" * 50)
        
        test_cases = [
            # (src, dst, msg, expected_suppression, description)
            (self.my_callsign, "20", "!WX", True, "Group ohne Target → lokal"),
            (self.my_callsign, "20", "!WX OE5HWN-12", False, "Group mit anderem Target → senden"),
            (self.my_callsign, "20", f"!WX {self.my_callsign}", True, "Group mit meinem Target → lokal"),
            (self.my_callsign, "TEST", "!WX", True, "Test-Gruppe ohne Target → lokal"),
            (self.my_callsign, "TEST", "!WX OE5HWN-12", False, "Test-Gruppe mit anderem Target → senden"),
            (self.my_callsign, "OE5HWN-12", "!TIME", True, "Persönlich ohne Target → lokal"),
            (self.my_callsign, "OE5HWN-12", "!TIME OE5HWN-12", False, "Persönlich mit Target (gleich dst) → senden"),
            (self.my_callsign, "OE5HWN-12", f"!TIME {self.my_callsign}", True, "Persönlich mit Target (ich) → lokal"),
            (self.my_callsign, "*", "!WX", True, "Ungültiges Ziel → suppress"),
            (self.my_callsign, "ALL", "!WX", True, "Ungültiges Ziel → suppress"),
            ("OE5HWN-12", "20", "!WX", False, "Nicht unsere Message → nicht suppessen"),
        ]
        
        results = []
        for src, dst, msg, expected, description in test_cases:
            test_data = {'src': src, 'dst': dst, 'msg': msg}
            normalized = self.validator.normalize_message_data(test_data)
            actual = self.validator.should_suppress_outbound(normalized)
            
            status = "✅ PASS" if actual == expected else "❌ FAIL"
            reason = self.validator.get_suppression_reason(normalized)
            
            results.append((status, description, actual, expected, reason))
            
            if has_console:
                print(f"{status} | {description}")
                print(f"     {src}→{dst} '{msg}' → {actual} (expected: {expected})")
                print(f"     Reason: {reason}")
                print()
        
        # Summary
        passed = sum(1 for r in results if r[0].startswith("✅"))
        total = len(results)
        
        if has_console:
            print(f"🧪 Test Summary: {passed}/{total} tests passed")
            if passed == total:
                print("🎉 All suppression tests passed!")
            else:
                print("⚠️ Some tests failed - check logic!")
            print("=" * 50)
        
        return passed == total



    def log_message_routing_decision(self, message_data, decision_type, action, reason):
        """Centralized logging for message routing decisions"""
        if not has_console:
            return
            
        src = message_data.get('src', 'unknown')
        dst = message_data.get('dst', 'unknown') 
        msg = message_data.get('msg', '')[:20] + ('...' if len(message_data.get('msg', '')) > 20 else '')
        
        print(f"🔄 {decision_type}: {src}→{dst} '{msg}' → {action} ({reason})")



    async def _storage_handler(self, routed_message):
        """Handle message storage for all routed messages"""
        if self.storage_handler:
            message_data = routed_message['data']

            src = message_data.get('src', '').split(',')[0].upper()
            if self._is_callsign_blocked(src):
                if has_console:
                    print(f"🚫 Blocked message from {src}")
                return

            raw_json = json.dumps(message_data)
            await self.storage_handler.store_message(message_data, raw_json)

    def _is_callsign_blocked(self, callsign):
        """Check if callsign is blocked"""
        # Get blocked list from CommandHandler
        command_handler = self.get_protocol('commands')
        if hasattr(command_handler, 'blocked_callsigns'):
            return callsign in command_handler.blocked_callsigns
        return False
            
    def register_protocol(self, name: str, handler):
        """Register a protocol handler (UDP, BLE, WebSocket)"""
        self._protocols[name] = handler
        if has_console:
            print(f"MessageRouter: Registered protocol '{name}'")
        
    def subscribe(self, message_type: str, handler_func):
        """Subscribe to specific message types"""
        self._subscribers[message_type].append(handler_func)
        if has_console:
            print(f"MessageRouter: {handler_func.__name__} subscribed to '{message_type}'")
        
    async def publish(self, source: str, message_type: str, data: dict):
        """Publish message from one protocol to all subscribers"""
        # Add routing metadata
        routed_message = {
            'source': source,
            'type': message_type,
            'data': data,
            'timestamp': int(time.time() * 1000)
        }
        
        # Send to all subscribers of this message type
        for handler in self._subscribers[message_type]:
            try:
                await handler(routed_message)

            except Exception as e:
                print(f"MessageRouter ERROR: Failed to route {message_type} to {handler.__name__}: {e}")
                
    def get_protocol(self, name: str):
        """Get a registered protocol handler"""
        return self._protocols.get(name)
        
    def list_subscriptions(self):
        """Debug: List all current subscriptions"""
        if has_console:
            print("MessageRouter subscriptions:")
            for msg_type, handlers in self._subscribers.items():
                handler_names = [h.__name__ for h in handlers]
                print(f"  {msg_type}: {handler_names}")

    async def route_command(self, command: str, websocket=None, MAC=None, BLE_Pin=None, **kwargs):
      """Route commands to appropriate protocol handlers"""
      if has_console:
        print(f"MessageRouter: Routing command '{command}'")
    
      try:
        # Message dump commands
        if command in ["send message dump", "send pos dump"]:
            await self._handle_message_dump_command(websocket)
            
        elif command == "mheard dump":
            await self._handle_mheard_dump_command(websocket)
            
        elif command == "dump to fs":
            await self._handle_dump_to_fs_command()
            
        # BLE commands
        elif command == "scan BLE":
            await self._handle_ble_scan_command()
            
        elif command == "BLE info":
            await self._handle_ble_info_command()
            
        elif command == "pair BLE":
            await self._handle_ble_pair_command(MAC, BLE_Pin)
            
        elif command == "unpair BLE":
            await self._handle_ble_unpair_command(MAC)
            
        elif command == "disconnect BLE":
            await self._handle_ble_disconnect_command()
            
        elif command == "connect BLE":
            await self._handle_ble_connect_command(MAC)
            
        elif command == "resolve-ip":
            await self._handle_resolve_ip_command(MAC)
            
        # Device commands (--commands)
        elif command.startswith("--setboostedgain"):
            await self._handle_device_a0_command(command)
            
        elif command.startswith("--set") or command.startswith("--sym"):
            await self._handle_device_set_command(command)
            
        elif command.startswith("--"):
            await self._handle_device_a0_command(command)
            
        else:
            print(f"MessageRouter: Unknown command '{command}'")
            if websocket:
                error_msg = {
                    'src_type': 'system',
                    'type': 'error', 
                    'msg': f"Unknown command: {command}",
                    'timestamp': int(time.time() * 1000)
                }
                await self.publish('router', 'websocket_message', error_msg)
                
      except Exception as e:
        print(f"MessageRouter ERROR: Failed to route command '{command}': {e}")
        if websocket:
            error_msg = {
                'src_type': 'system',
                'type': 'error',
                'msg': f"Command failed: {command} - {str(e)}",
                'timestamp': int(time.time() * 1000)
            }
            await self.publish('router', 'websocket_message', error_msg)

    async def _handle_message_dump_command(self, websocket):
        """Handle message dump commands"""
        # Get initial payload
        preview = {
            "type": "response",
            "msg": "message dump", 
            "data": self.storage_handler.get_initial_payload()
        }
        await self.publish('router', 'websocket_direct', {'websocket': websocket, 'data': preview})
        await asyncio.sleep(0)

        # Send full dump in chunks
        CHUNK_SIZE = 20000
        full_data = self.storage_handler.get_full_dump()
        total = len(full_data)
        
        if has_console:
            print("total:", total)

        for i in range(0, total, CHUNK_SIZE):
            if has_console:
                print("sending message chunk ", i)
            chunk = full_data[i:i+CHUNK_SIZE]
            full = {
                "type": "response",
                "msg": "message dump",
                "data": chunk
            }
            await self.publish('router', 'websocket_direct', {'websocket': websocket, 'data': full})
            await asyncio.sleep(0)

    async def _handle_mheard_dump_command(self, websocket):
        """Handle mheard dump command"""
        # Use the parallel version
        mheard = await self.storage_handler.process_mheard_store_parallel()
        payload = {
            "type": "response",
            "msg": "mheard stats", 
            "data": mheard
        }
        await self.publish('router', 'websocket_direct', {'websocket': websocket, 'data': payload})


    async def _handle_dump_to_fs_command(self):
        """Handle dump to filesystem command"""
        self.storage_handler.save_dump(store_file_name)
        print(f"Daten gespeichert in {store_file_name}")

    # BLE command handlers
    async def _handle_ble_scan_command(self):
        """Handle BLE scan command"""
        await scan_ble_devices(message_router=self)

    async def _handle_ble_pair_command(self, MAC, BLE_Pin):
        """Handle BLE pair command"""
        await ble_pair(MAC, BLE_Pin, message_router=self)

    async def _handle_ble_unpair_command(self, MAC):
        """Handle BLE unpair command"""
        await ble_unpair(MAC, message_router=self)

    async def _handle_ble_connect_command(self, MAC):
        """Handle BLE connect command"""
        await ble_connect(MAC, message_router=self)

    async def _handle_ble_disconnect_command(self):
        """Handle BLE disconnect command"""
        await ble_disconnect(message_router=self)

    async def _handle_resolve_ip_command(self, hostname):
        """Handle resolve IP command"""
        await backend_resolve_ip(hostname, message_router=self)

    # Device command handlers
    async def _handle_device_a0_command(self, command):
        """Handle device A0 commands (--pos, --reboot, etc.)"""
        await handle_a0_command(command)

    async def _handle_device_set_command(self, command):
        """Handle device set commands (--settime, --setCALL, etc.)"""
        await handle_set_command(command)

    def _should_suppress_outbound(self, message_data):
        """Check if outbound message should be suppressed using validator"""
        if not self.validator:
            if has_console:
                print(f"⚠️ Validator not initialized, no suppression")
            return False
        
        suppress = self.validator.should_suppress_outbound(message_data)
        
        if has_console:
            reason = self.validator.get_suppression_reason(message_data)
            action = "SUPPRESS" if suppress else "FORWARD"
            print(f"🔄 Suppression decision: {action} - {reason}")
        
        return suppress



    async def _udp_message_handler(self, routed_message):
        """Handle UDP messages from WebSocket and route to UDP handler"""
        message_data = routed_message['data']
    
        # EARLY NORMALIZATION - ab hier alles uppercase
        normalized_data = self.validator.normalize_message_data(message_data)
        
        # Add our callsign if missing
        if not normalized_data.get('src') and self.my_callsign:
            normalized_data['src'] = self.my_callsign
    
        if has_console:
            print(f"📡 UDP Handler: Processing '{normalized_data.get('msg')}' from {normalized_data.get('src')} to {normalized_data.get('dst')}")
    
        if self._should_suppress_outbound(normalized_data):
            reason = self.validator.get_suppression_reason(normalized_data)
            self.log_message_routing_decision(normalized_data, "UDP_SUPPRESSION", "SUPPRESS", reason)
            
            synthetic_message = self._create_synthetic_message(normalized_data, 'udp')
            await self._route_to_command_handler(synthetic_message)
            return
    
        # Check if this is a self-message first
        is_self_message = await self._handle_outgoing_message(normalized_data, 'udp')
    
        if is_self_message:
            if has_console:
                print(f"📡 UDP Handler: Self-message handled, not sending to mesh")
            return
    
        # External message - send to mesh network
        if has_console:
            print(f"📡 UDP Handler: Sending external message to mesh network")
            
        udp_handler = self.get_protocol('udp')
        
        if udp_handler:
            try:
                await udp_handler.send_message(normalized_data)
                if has_console:
                    print(f"📡 UDP message sent successfully to mesh network")
            except Exception as e:
                print(f"📡 UDP message send failed: {e}")
                await self.publish('system', 'websocket_message', {
                    'src_type': 'system',
                    'type': 'error',
                    'msg': f"Failed to send UDP message: {e}",
                    'timestamp': int(time.time() * 1000)
                })
        else:
            print(f"📡 UDP handler not available, can't send message")
            await self.publish('system', 'websocket_message', {
                'src_type': 'system',
                'type': 'error', 
                'msg': "UDP handler not available",
                'timestamp': int(time.time() * 1000)
            })

    
    async def _ble_message_handler(self, routed_message):
        """Handle BLE messages from WebSocket and route to BLE client"""
        
        message_data = routed_message['data']
        
        # EARLY NORMALIZATION - ab hier alles uppercase
        normalized_data = self.validator.normalize_message_data(message_data)
        
        # Add our callsign if missing
        if not normalized_data.get('src') and self.my_callsign:
            normalized_data['src'] = self.my_callsign
    
        msg = normalized_data.get('msg')
        dst = normalized_data.get('dst')
        
        if has_console:
            print(f"📱 BLE Handler: Processing '{msg}' from {normalized_data.get('src')} to '{dst}'")
    
        if self._should_suppress_outbound(normalized_data):
            reason = self.validator.get_suppression_reason(normalized_data)
            self.log_message_routing_decision(normalized_data, "BLE_SUPPRESSION", "SUPPRESS", reason)
            
            synthetic_message = self._create_synthetic_message(normalized_data, 'ble')
            await self._route_to_command_handler(synthetic_message)
            return
    
        # Check if this is a self-message first
        is_self_message = await self._handle_outgoing_message(normalized_data, 'ble')
        
        if is_self_message:
            if has_console:
                print(f"📱 BLE Handler: Self-message handled, not sending to device")
            return
        
        # External message - send to BLE device
        if has_console:
            print(f"📱 BLE Handler: Sending external message to BLE device")
        await handle_ble_message(msg, dst)
    
    def _is_message_to_self(self, message_data):
        """Check if message is addressed to our own callsign (assumes normalized data)"""
        if not self.my_callsign:
            return False
        dst = message_data.get('dst', '')
        return dst == self.my_callsign
    
    def _create_synthetic_message(self, original_message, protocol_type='udp'):
        """Create a synthetic message that looks like it came from LoRa (uses normalized data)"""
        current_time = int(time.time())
        msg_id = f"{current_time:08X}"
    
        return {
            'src': original_message.get('src'),  # Already uppercase
            'dst': original_message.get('dst'),  # Already uppercase  
            'msg': original_message.get('msg'),
            'msg_id': msg_id,
            'type': 'msg',
            'src_type': protocol_type,
            'timestamp': current_time * 1000
        }



    async def _handle_outgoing_message(self, message_data, protocol_type='udp'):
        """Unified handler for outgoing messages - handles self-message detection"""
        
        if self._is_message_to_self(message_data):
            if has_console:
                print(f"🔄 MessageRouter: Detected self-message to {message_data.get('dst')}, routing to CommandHandler only")
            synthetic_message = self._create_synthetic_message(message_data)
            await self._route_to_command_handler(synthetic_message)
            return True  # Indicates message was handled as self-message
        
        return False  # Indicates message should be sent to external protocol

    async def _route_to_command_handler(self, synthetic_message):
        """Route synthetic message to CommandHandler"""
        if has_console:
            print(f"🔄 MessageRouter: Creating synthetic message: {synthetic_message}")

        routed_message = {
            'source': 'self',
            'type': 'ble_notification',
            'data': synthetic_message,
            'timestamp': int(time.time() * 1000)
        }

        if has_console:
            print(f"🔄 MessageRouter: Routing to CommandHandler subscribers...")
            print(f"🔄 MessageRouter: Available subscribers for 'ble_notification': {len(self._subscribers['ble_notification'])}")
    
        # Find CommandHandler subscribers
        for handler in self._subscribers['ble_notification']:
            try:
                  await handler(routed_message)
                  if has_console:
                      print(f"🔄 MessageRouter: Routed self-message to CommandHandler")
            except Exception as e:
                    print(f"MessageRouter ERROR: Failed to route self-message: {e}")
                


class MessageValidator:
    """Centralized message validation and normalization"""
    
    def __init__(self, my_callsign):
        self.my_callsign = my_callsign.upper()
    
    def normalize_message_data(self, message_data):
        """Normalize message data - uppercase and validate early"""
        normalized = message_data.copy()
        
        # Defensive uppercase normalization
        src_raw = message_data.get('src', '').strip()
        dst_raw = message_data.get('dst', '').strip()
        msg_raw = message_data.get('msg', '').strip()
        
        # Handle comma-separated src (path routing)
        src = src_raw.split(',')[0].upper() if ',' in src_raw else src_raw.upper()
        dst = dst_raw.upper()
        
        # Normalize command to uppercase while preserving structure
        msg = msg_raw.upper() if msg_raw.startswith('!') else msg_raw
        
        normalized.update({
            'src': src,
            'dst': dst,
            'msg': msg
        })
        
        if has_console and (src != src_raw or dst != dst_raw):
            print(f"🔧 Normalized: src='{src_raw}'→'{src}', dst='{dst_raw}'→'{dst}'")
        
        return normalized

    def extract_target_callsign(self, msg):
        """Extract target callsign from command message"""
        if not msg or not msg.startswith('!'):
            return None
        
        # Ensure message is uppercase for processing
        msg_upper = msg.upper().strip()
        parts = msg_upper.split()
        
        if len(parts) < 2:
            return None

        command = parts[0][1:]

        if command == 'CTCPING':
            # Look for target:CALLSIGN pattern for execution delegation
            for part in parts[1:]:
                if part.startswith('TARGET:'):  # ← FIXED!
                    potential_target = part[7:]  # Remove 'TARGET:' prefix
                    if potential_target.upper() in ['LOCAL', '']:
                        return None  # Local execution
                    # Validate callsign pattern  
                    if re.match(r'^[A-Z0-9]{2,8}(-\d{1,2})?$', potential_target):
                        if has_console:
                            print(f"🎯 CTCPING target extracted: '{potential_target}' from '{msg}'")
                        return potential_target
        
            # No target parameter = local execution
            return None

        
        # Look for target in last part (pattern: !WX DK5EN-15)
        potential_target = parts[-1].strip()
        
        # Validate callsign pattern (letters/numbers, optional SID)
        if re.match(r'^[A-Z0-9]{2,8}(-\d{1,2})?$', potential_target):
            if has_console:
                print(f"🎯 Target extracted: '{potential_target}' from '{msg}'")
            return potential_target
        
        if has_console:
            print(f"🎯 No valid target in: '{msg}' (checked: '{potential_target}')")
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

    def is_valid_destination(self, dst):
        """Validate destination format (assumes already uppercase)"""
        if not dst:
            if has_console:
                print(f"🔍 Invalid dst: empty")
            return False
        
        # Invalid destinations from table
        invalid_destinations = ['*', 'ALL', '']
        if dst in invalid_destinations:
            if has_console:
                print(f"🔍 Invalid dst: '{dst}' in blacklist")
            return False
        
        # Valid: callsign pattern
        if re.match(r'^[A-Z0-9]{2,8}(-\d{1,2})?$', dst):
            if has_console:
                print(f"🔍 Valid dst: '{dst}' matches callsign pattern")
            return True
        
        # Valid: group pattern
        if self.is_group(dst):
            if has_console:
                print(f"🔍 Valid dst: '{dst}' is group")
            return True
        
        if has_console:
            print(f"🔍 Invalid dst: '{dst}' no pattern match")

        return False

    def is_command(self, msg):
        """Check if message is a command"""
        return msg and msg.startswith('!')

    def is_self_message(self, src, dst):
        """Check if message is from us to us"""
        return src == self.my_callsign and dst == self.my_callsign


    def should_suppress_outbound(self, message_data):
        """Implement simplified suppression logic from table"""
        src = message_data.get('src', '')
        dst = message_data.get('dst', '')
        msg = message_data.get('msg', '')
        
        if has_console:
            print(f"🔍 Suppression check: src='{src}', dst='{dst}', msg='{msg[:20]}...'")
        
        # Only check our own outgoing commands
        if src != self.my_callsign:
            if has_console:
                print(f"🔍 → NOT our message ({src} != {self.my_callsign}) - NO SUPPRESSION")
            return False
        
        # Must be a command
        if not self.is_command(msg):
            if has_console:
                print(f"🔍 → Not a command - NO SUPPRESSION")
            return False
        
        # Invalid destinations always suppress
        if not self.is_valid_destination(dst):
            if has_console:
                print(f"🔍 → Invalid destination '{dst}' - SUPPRESS")
            return True
        
        target = self.extract_target_callsign(msg)
        
        # No target → execute locally
        if not target:
            if has_console:
                print(f"🔍 → No target in '{msg}' - SUPPRESS (local execution)")
            return True
        
        # Target is us → execute locally
        if target == self.my_callsign:
            if has_console:
                print(f"🔍 → Target is us ({target}) - SUPPRESS (local execution)")
            return True
        
        # Target is someone else → send to mesh
        if has_console:
            print(f"🔍 → Target is '{target}' (not us) - NO SUPPRESSION (send to mesh)")
        return False

    def get_suppression_reason(self, message_data):
        """Get human-readable reason for suppression decision"""
        src = message_data.get('src', '')
        dst = message_data.get('dst', '')
        msg = message_data.get('msg', '')
        
        if src != self.my_callsign:
            return f"Not our message ({src})"
        
        if not self.is_command(msg):
            return "Not a command"
        
        if not self.is_valid_destination(dst):
            return f"Invalid destination ({dst})"
        
        target = self.extract_target_callsign(msg)
        
        if not target:
            return "No target → local execution"
        
        if target == self.my_callsign:
            return f"Target is us ({target}) → local execution"
        
        return f"Target is {target} → send to mesh"


# In command_handler.py - neue Test-Methode hinzufügen:

def test_kickban_logic(self):
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
            #result = await self.handle_kickban(args, requester)
            result = self.handle_kickban(args, requester)
            
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

# Auch eine Test-Methode für die Message-Blocking Integration:
def test_message_blocking_integration(self):
    """Test message blocking integration with MessageRouter"""
    if has_console:
        print("\n🧪 Testing Message Blocking Integration:")
        print("=" * 45)
    
    # This would test the MessageRouter integration
    # For now, just a placeholder that tests the logic
    test_callsigns = [
        ("OE1ABC-5", True, "Normal callsign should pass"),
        ("W1XYZ-1", True, "Different callsign should pass"),  
        ("INVALID", False, "Invalid callsign should be handled"),
    ]
    
    results = []
    for callsign, should_pass, description in test_callsigns:
        # Test the blocking logic
        self.blocked_callsigns = {"OE1ABC-5"}  # Block OE1ABC-5
        
        # Simulate checking if callsign is blocked
        is_blocked = callsign in self.blocked_callsigns
        result_correct = (not is_blocked) == should_pass
        
        status = "✅ PASS" if result_correct else "❌ FAIL"
        results.append((status, description, result_correct))
        
        if has_console:
            print(f"{status} | {description}")
            print(f"     Callsign: {callsign}, Blocked: {is_blocked}, Should pass: {should_pass}")
    
    passed = sum(1 for r in results if r[2])
    total = len(results)
    
    if has_console:
        print(f"🧪 Blocking Integration Summary: {passed}/{total} tests passed")
        print("=" * 45)





async def main():
    message_store = deque()
    storage_handler = MessageStorageHandler(message_store, MAX_STORE_SIZE_MB)

    storage_handler.load_dump(store_file_name)
    storage_handler.prune_messages(PRUNE_HOURS, block_list)

    message_router = MessageRouter(storage_handler)

    CALL_SIGN = config["CALL_SIGN"]
    message_router.set_callsign(CALL_SIGN)

    #Command Handler Plugin
    command_handler = create_command_handler(message_router, storage_handler, CALL_SIGN, LAT, LONG, STAT_NAME, USER_INFO_TEXT)

    message_router.register_protocol('commands', command_handler)

    udp_handler = UDPHandler(
        listen_port=UDP_PORT_list,
        target_host=config["UDP_TARGET"], 
        target_port=UDP_PORT_send,
        message_callback=None,
        message_router=message_router
    )

    message_router.register_protocol('udp', udp_handler)

    websocket_manager = WebSocketManager(WS_HOST, WS_PORT, message_router)
    message_router.register_protocol('websocket', websocket_manager)

    await udp_handler.start_listening()
    
    try:
          await websocket_manager.start_server()
    except OSError as e:
      if e.errno == errno.EADDRINUSE:
           print(f"❌ Address {WS_HOST}:{WS_PORT} already in use.")
           print("🧠 Tip: Is another instance of the server already running?")
           print("👀 Try `lsof -i :{}` or `netstat -tulpen | grep {}` to investigate.".format(WS_PORT, WS_PORT))
           print("💣 Exiting gracefully from a non recoverable error.\n")
           sys.exit(1)
      else:
            raise

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def stdin_reader():
        while True:
            line = sys.stdin.readline()
            if not line:
                time.sleep(1)
                continue
            if line.strip() == "q":
                loop.call_soon_threadsafe(stop_event.set)
                break

    # Signal handling with fallback
    shutdown_requested = False

    def handle_shutdown(signum=None, frame=None):
        print(f"🛡️ Signal {signum or 'SIGINT'} received, stopping proxy service ..")
        if stop_event.is_set():
            print("🛡️ Force shutdown - second signal received")
            import os
            os._exit(1)  # Force exit if called twice
        stop_event.set()
    
    # Try asyncio signal handlers first (preferred)
    try:
        loop.add_signal_handler(signal.SIGINT, handle_shutdown)
        loop.add_signal_handler(signal.SIGTERM, handle_shutdown)
        signal_method = "asyncio"
    except Exception as e:
        # Fallback to traditional signal handlers
        print(f"⚠️ Could not set asyncio signal handlers: {e}")
        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)
        signal_method = "traditional"
    
    if has_console:
        print(f"🛡️ Signal handling: {signal_method}")
    

    if sys.stdin.isatty():
       print("Drücke 'q' + Enter zum Beenden und Speichern")
       loop.run_in_executor(None, stdin_reader)

    print(f"UDP-Listen {UDP_PORT_list}, Target MeshCom {UDP_TARGET}")
    print(f"MessageRouter: {len(message_router._subscribers)} message types, {len(message_router._protocols)} protocols")


########### debug
#
#
#    signal.signal(signal.SIGUSR1, debug_signal_handler)
#    print("🔍 DEBUG: Send 'kill -USR1 <pid>' to get stack trace")
#
#
########### debug


    suppression_passed = True  # Default values
    command_handler_passed = True

    if has_console:
        print("\n🧪 Running suppression logic tests...")
        suppression_passed = message_router.test_suppression_logic()
    
        print("\n🧪 Running command handler test suite...")
        command_handler_passed = await command_handler.run_all_tests()  # Alle Tests auf einmal
    
        if suppression_passed and command_handler_passed:
            print("\n🎉 All tests passed! System ready.")
        else:
            print("\n⚠️ Some tests failed. Check implementation.")

### unit tests
    



    await stop_event.wait()
    
    print("🛑 Stopping proxy server, saving to disc ..")


    try:
        # Step 1: Clean up beacons
        print("🛑 Stopping beacon tasks...")
        await asyncio.wait_for(
            command_handler.cleanup_topic_beacons(), 
            timeout=5.0
        )
    except asyncio.TimeoutError:
        print("⚠️ Beacon cleanup timeout")

    
    # Clean shutdown sequence with timeouts
    try:
        # Step 2: Disconnect BLE with timeout
        print("🛑 Disconnecting BLE...")
        await asyncio.wait_for(
            message_router.route_command("disconnect BLE"), 
            timeout=5.0
        )
    except asyncio.TimeoutError:
        print("⚠️ BLE disconnect timeout")
    
    try:
        # Step 3: Stop UDP handler
        print("🛑 Stopping UDP handler...")
        await asyncio.wait_for(udp_handler.stop_listening(), timeout=3.0)
    except asyncio.TimeoutError:
        print("⚠️ UDP stop timeout")
    
    try:
        # Step 4: Stop WebSocket server
        print("🛑 Stopping WebSocket server...")
        await asyncio.wait_for(websocket_manager.stop_server(), timeout=3.0)
    except asyncio.TimeoutError:
        print("⚠️ WebSocket stop timeout")
    
    print("🛑 All services stopped")
    
    # Save data
    try:
        storage_handler.save_dump(store_file_name)
        print("✅ Data saved successfully")
    except Exception as e:
        print(f"⚠️ Error saving data: {e}")
    
    print("✅ Shutdown complete")

    # Force clean process exit after successful cleanup
    import os
    os._exit(0)


if __name__ == "__main__":

    has_console = sys.stdout.isatty()
    config = load_config()

    LAT = config.get("LAT")
    LONG = config.get("LONG")
    STAT_NAME = config.get("STAT_NAME")
    print(f"WX Service for {STAT_NAME} {LAT}/{LONG}")

    USER_INFO_TEXT = config.get("USER_INFO_TEXT")

    UDP_PORT_list = config["UDP_PORT_list"]
    UDP_PORT_send = config["UDP_PORT_send"]

    UDP_TARGET = (config["UDP_TARGET"], UDP_PORT_send)

    WS_HOST = config["WS_HOST"]
    WS_PORT = config["WS_PORT"]

    PRUNE_HOURS = config["PRUNE_HOURS"]
    print(f"Messages older than {hours_to_dd_hhmm(PRUNE_HOURS)} get deleted")

    MAX_STORE_SIZE_MB = config["MAX_STORAGE_SIZE_MB"]
    print(f"Messages store limited to {MAX_STORE_SIZE_MB}MB")

    store_file_name = config["STORE_FILE_NAME"]
    print(f"Messages will be stored on exit: {store_file_name}")

    #dumper = DailySQLiteDumper()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
       print("Manuell beendet mit Ctrl+C")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")

