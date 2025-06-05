#!/usr/bin/env python3
import asyncio
import json
import time
import sys
from collections import defaultdict, deque

VERSION = "v0.38.0"

has_console = sys.stdout.isatty()

# Command registry with handler functions and metadata
COMMANDS = {
    'search': {
        'handler': 'handle_search',
        'args': ['user', 'days', 'limit'],
        'format': '!search user:CALL days:N limit:N',
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
MAX_RESPONSE_LENGTH = 200  # Maximum characters per message chunk
MAX_CHUNKS = 3            # Maximum number of response chunks


class CommandHandler:
    def __init__(self, message_router=None, storage_handler=None):
        self.message_router = message_router
        self.storage_handler = storage_handler
        self.my_callsign = "DK5EN-99"  # Your callsign to filter commands
        
        # Subscribe to message types that might contain commands
        if message_router:
            message_router.subscribe('mesh_message', self._message_handler)
            message_router.subscribe('ble_notification', self._message_handler)
            
        if has_console:
            print(f"CommandHandler: Initialized with {len(COMMANDS)} commands")

    async def _message_handler(self, routed_message):
        """Handle incoming messages and check for commands"""
        message_data = routed_message['data']
        
        # Filter for messages directed to us
        dst = message_data.get('dst')
        if dst != self.my_callsign:
            return
            
        msg_text = message_data.get('msg', '')
        src = message_data.get('src', 'unknown')
        
        # Check if message contains a command
        if not msg_text.startswith('!'):
            return
            
        if has_console:
            print(f"📋 CommandHandler: Processing command '{msg_text}' from {src}")
            
        # Parse and execute command
        try:
            cmd_result = self.parse_command(msg_text)
            if cmd_result:
                cmd, kwargs = cmd_result
                response = await self.execute_command(cmd, kwargs, src)
                await self.send_response(response, src)
            else:
                await self.send_response("❌ Unknown command. Try !help", src)
                
        except Exception as e:
            print(f"CommandHandler ERROR: {e}")
            await self.send_response(f"❌ Command failed: {str(e)[:50]}", src)

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
                    kwargs['user'] = part
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
                elif cmd == 'pos' and not kwargs:
                    kwargs['call'] = part
                        
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
        """Search messages by user and timeframe"""
        user = kwargs.get('user', '*')
        days = int(kwargs.get('days', 1))
        limit = int(kwargs.get('limit', 10))
        
        if not self.storage_handler:
            return "❌ Message storage not available"
            
        # Search through message store
        results = []
        cutoff_time = time.time() - (days * 24 * 60 * 60)
        
        for item in reversed(list(self.storage_handler.message_store)):
            if len(results) >= limit:
                break
                
            try:
                raw_data = json.loads(item["raw"])
                timestamp = raw_data.get('timestamp', 0)
                
                # Skip old messages
                if timestamp < cutoff_time * 1000:
                    continue
                    
                src = raw_data.get('src', '')
                msg = raw_data.get('msg', '')
                
                # Filter by user if specified
                if user != '*' and user.upper() not in src.upper():
                    continue
                    
                # Format result
                time_str = time.strftime('%H:%M', time.localtime(timestamp/1000))
                results.append(f"{time_str} {src}: {msg[:50]}")
                
            except (json.JSONDecodeError, KeyError):
                continue
                
        if not results:
            return f"🔍 No messages found for {user} in last {days} day(s)"
            
        response = f"🔍 Found {len(results)} messages:\n"
        response += "\n".join(results)
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
                elif msg_type == 'pos':
                    pos_count += 1
                    
                if src:
                    users.add(src.split(',')[0])  # First callsign in path
                    
            except (json.JSONDecodeError, KeyError):
                continue
                
        total = msg_count + pos_count
        avg_per_hour = round(total / max(hours, 1), 1)
        
        response = f"📊 Stats (last {hours}h):\n"
        response += f"Messages: {msg_count}\n"
        response += f"Positions: {pos_count}\n"
        response += f"Total: {total} ({avg_per_hour}/h)\n"
        response += f"Active stations: {len(users)}"
        
        return response

    async def handle_mheard(self, kwargs, requester):
        """Show recently heard stations"""
        limit = int(kwargs.get('limit', 10))
        
        if not self.storage_handler:
            return "❌ Message storage not available"
            
        # Get recent station activity
        stations = defaultdict(lambda: {'last_seen': 0, 'msg_count': 0})
        
        for item in list(self.storage_handler.message_store)[-1000:]:  # Last 1000 messages
            try:
                raw_data = json.loads(item["raw"])
                src = raw_data.get('src', '')
                timestamp = raw_data.get('timestamp', 0)
                
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
            
        response = "📻 Recently heard:\n"
        for call, data in sorted_stations:
            last_time = time.strftime('%H:%M', time.localtime(data['last_seen']/1000))
            response += f"{call} @{last_time} ({data['msg_count']})\n"
            
        return response.rstrip()

    async def handle_position(self, kwargs, requester):
        """Show position data for callsign"""
        call = kwargs.get('call', '').upper()
        days = int(kwargs.get('days', 1))
        
        if not call:
            return "❌ Callsign required. Use: !pos call:CALL"
            
        if not self.storage_handler:
            return "❌ Message storage not available"
            
        cutoff_time = time.time() - (days * 24 * 60 * 60)
        positions = []
        
        for item in reversed(list(self.storage_handler.message_store)):
            try:
                raw_data = json.loads(item["raw"])
                timestamp = raw_data.get('timestamp', 0)
                
                if timestamp < cutoff_time * 1000:
                    continue
                    
                if raw_data.get('type') != 'pos':
                    continue
                    
                src = raw_data.get('src', '')
                if call not in src.upper():
                    continue
                    
                lat = raw_data.get('lat')
                lon = raw_data.get('long')
                alt = raw_data.get('alt')
                
                if lat is not None and lon is not None:
                    time_str = time.strftime('%H:%M', time.localtime(timestamp/1000))
                    pos_str = f"{time_str}: {lat:.4f},{lon:.4f}"
                    if alt:
                        pos_str += f" {alt}m"
                    positions.append(pos_str)
                    
                if len(positions) >= 5:  # Limit to 5 recent positions
                    break
                    
            except (json.JSONDecodeError, KeyError):
                continue
                
        if not positions:
            return f"📍 No position data for {call} in last {days} day(s)"
            
        response = f"📍 {call} positions:\n"
        response += "\n".join(positions)
        return response

    async def handle_help(self, kwargs, requester):
        """Show available commands"""
        response = "📋 Available commands:\n"
        for cmd, info in COMMANDS.items():
            response += f"{info['format']}\n"
            
        response += "\nExamples:\n"
        response += "!search user:DO2QG days:7\n"
        response += "!stats 24\n"
        response += "!mheard 5"
        
        return response

    async def send_response(self, response, recipient):
        """Send response back to requester, chunking if necessary"""
        if not response:
            return
            
        # Split response into chunks if too long
        chunks = self._chunk_response(response)
        
        for i, chunk in enumerate(chunks[:MAX_CHUNKS]):
            if len(chunks) > 1:
                chunk_header = f"({i+1}/{min(len(chunks), MAX_CHUNKS)}) "
                chunk = chunk_header + chunk
                
            # Send via message router
            if self.message_router:
                message_data = {
                    'dst': recipient,
                    'msg': chunk,
                    'src_type': 'command_response',
                    'type': 'msg'
                }
                
                # Route to appropriate protocol (BLE or UDP)
                await self.message_router.publish('command', 'ble_message', message_data)
                
                # Small delay between chunks
                if i < len(chunks) - 1:
                    await asyncio.sleep(1)
                    
            if has_console:
                print(f"📋 CommandHandler: Sent response chunk {i+1} to {recipient}")

    def _chunk_response(self, response):
        """Split response into chunks that fit message size limits"""
        if len(response) <= MAX_RESPONSE_LENGTH:
            return [response]
            
        chunks = []
        lines = response.split('\n')
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
def create_command_handler(message_router, storage_handler):
    """Factory function to create and integrate CommandHandler"""
    return CommandHandler(message_router, storage_handler)
