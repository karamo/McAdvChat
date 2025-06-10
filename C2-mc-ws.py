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

VERSION="v0.46.0"

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

        if message_storage_handler:
            self.subscribe('mesh_message', self._storage_handler)
            self.subscribe('ble_notification', self._storage_handler)

        self.subscribe('ble_message', self._ble_message_handler)  
        self.subscribe('udp_message', self._udp_message_handler) 

    def set_callsign(self, callsign):
        """Set the callsign from config"""
        self.my_callsign = callsign.upper()

    async def _udp_message_handler(self, routed_message):
        """Handle UDP messages from WebSocket and route to UDP handler"""
        message_data = routed_message['data']

        if has_console:
            print(f"📡 UDP Message Handler: Processing message to {message_data.get('dst')}")
    
        # Check if this is a self-message first
        is_self_message = await self._handle_outgoing_message(message_data, 'udp')
    
        if is_self_message:
            if has_console:
                print(f"📡 UDP Message Handler: Self-message handled, not sending to mesh")
            return
    
        # External message - send to mesh network
        if has_console:
            print(f"📡 UDP Message Handler: Sending external message to mesh network")
            
        # Get the UDP protocol handler
        udp_handler = self.get_protocol('udp')
        
        if udp_handler:
            try:
                await udp_handler.send_message(message_data)
                if has_console:
                    print(f"📡 UDP message sent successfully to mesh network")
            except Exception as e:
                print(f"📡 UDP message send failed: {e}")
                # Optionally publish error status
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
        msg = message_data.get('msg')
        dst = message_data.get('dst')
        
        if has_console:
            print(f"📱 BLE Message Handler: Processing message '{msg}' to '{dst}'")
    
        # Check if this is a self-message first
        is_self_message = await self._handle_outgoing_message(message_data, 'ble')
        
        if is_self_message:
            if has_console:
                print(f"📱 BLE Message Handler: Self-message handled, not sending to device")
            return
        
        # External message - send to BLE device
        if has_console:
            print(f"📱 BLE Message Handler: Sending external message to BLE device")
        await handle_ble_message(msg, dst)
        


    async def _storage_handler(self, routed_message):
        """Handle message storage for all routed messages"""
        if self.storage_handler:
            message_data = routed_message['data']
            raw_json = json.dumps(message_data)
            await self.storage_handler.store_message(message_data, raw_json)
            
            #if has_console:
            #    count = self.storage_handler.get_message_count()
            #    size_mb = self.storage_handler.get_storage_size_mb()
            #    print(f"📦 MessageStorage: {count} messages, {size_mb:.2f}MB")
        
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
        
        #if has_console:
        #    print(f"MessageRouter: Publishing {message_type} from {source} to {len(self._subscribers[message_type])} subscribers")
        
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

    #async def _handle_ble_info_command(self):
    #    """Handle BLE info command"""
    #    global client
    #    if client is None:
    #        await self.publish('ble', 'ble_status', {
    #            'src_type': 'BLE',
    #            'TYP': 'blueZ', 
    #            'command': 'ble_info result',
    #            'result': 'error',
    #            'msg': "client not connected",
    #            'timestamp': int(time.time() * 1000)
    #        })
    #        return
    #    await client.ble_info()

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

    def _is_message_to_self(self, message_data):
        """Check if message is addressed to our own callsign"""
        if not self.my_callsign:
            return False
        dst = message_data.get('dst', '').upper()
        msg = message_data.get('msg', '')

        #return dst == self.my_callsign and msg.startswith('!')
        return dst == self.my_callsign

    def _create_synthetic_message(self, original_message, protocol_type='udp'):
        """Create a synthetic message that looks like it came from LoRa"""
        current_time = int(time.time())
        msg_id = f"{current_time:08X}"  # Hex timestamp as msg_id
    
        return {
            'src': self.my_callsign,  # Use configured callsign as source
            'dst': original_message.get('dst').upper(),
            'msg': original_message.get('msg'),
            'msg_id': msg_id,
            'type': 'msg',
            'src_type': protocol_type,  # Use the actual protocol type
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
            #if 'CommandHandler' in str(type(handler)):
            try:
                  await handler(routed_message)
                  if has_console:
                      print(f"🔄 MessageRouter: Routed self-message to CommandHandler")
            except Exception as e:
                    print(f"MessageRouter ERROR: Failed to route self-message: {e}")
                

async def main():
    message_store = deque()
    storage_handler = MessageStorageHandler(message_store, MAX_STORE_SIZE_MB)

    storage_handler.load_dump(store_file_name)
    storage_handler.prune_messages(PRUNE_HOURS, block_list)

    message_router = MessageRouter(storage_handler)

    CALL_SIGN = config["CALL_SIGN"]
    message_router.set_callsign(CALL_SIGN)

    #Command Handler Plugin
    command_handler = create_command_handler(message_router, storage_handler, CALL_SIGN, LAT, LONG, STAT_NAME)

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

    #print(f"WebSocket ws://{WS_HOST}:{WS_PORT}")
    print(f"UDP-Listen {UDP_PORT_list}, Target MeshCom {UDP_TARGET}")
    print(f"MessageRouter: {len(message_router._subscribers)} message types, {len(message_router._protocols)} protocols")

    await stop_event.wait()
    
    print("🛑 Stopping proxy server, saving to disc ..")
    
    # Clean shutdown sequence with timeouts
    try:
        # Step 1: Disconnect BLE with timeout
        print("🛑 Disconnecting BLE...")
        await asyncio.wait_for(
            message_router.route_command("disconnect BLE"), 
            timeout=5.0
        )
    except asyncio.TimeoutError:
        print("⚠️ BLE disconnect timeout")
    
    try:
        # Step 2: Stop UDP handler
        print("🛑 Stopping UDP handler...")
        await asyncio.wait_for(udp_handler.stop_listening(), timeout=3.0)
    except asyncio.TimeoutError:
        print("⚠️ UDP stop timeout")
    
    try:
        # Step 3: Stop WebSocket server
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

    LAT = config["LAT"]
    LONG = config["LONG"]
    STAT_NAME = config["STAT_NAME"]
    print(f"WX Service for {STAT_NAME} {LAT}/{LONG}")


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

