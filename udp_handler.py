#!/usr/bin/env python3
import asyncio
import socket
import json
import time
import unicodedata

VERSION="v0.48.0"


def is_allowed_char(ch: str) -> bool:
    """Check if character is allowed in our charset"""
    codepoint = ord(ch)

    # Explicit whitelist European Umlaut
    if ch in "äöüÄÖÜßäàáâãåāéèêëėîïíīìôòóõōûùúūÀÁÂÃÅĀÉÈÊËĖÎÏÍĪÌÔÒÓÕŌÜÛÙÚŪśšŚŠÿçćčñń":
        return True

    if ch in "⁰":
        return True
    
    # ASCII 0x20 to 0x5C inclusive
    if 0x20 <= codepoint <= 0x5C:
        return True
    
    # Allow up to 0x7E?
    if 0x5D <= codepoint <= 0x7E:
        return True

    # Allow Emoji Variation Selector
    if codepoint == 0xFE0F:
        return True  # critical for full-color emoji rendering

    # Reject surrogates, noncharacters
    if 0xD800 <= codepoint <= 0xDFFF:
        return False

    if codepoint & 0xFFFF in [0xFFFE, 0xFFFF]:
        return False
    
    # Reject private use areas
    if (0xE000 <= codepoint <= 0xF8FF) or (0xF0000 <= codepoint <= 0xFFFFD) or (0x100000 <= codepoint <= 0x10FFFD):
        return False

    # Accept emojis and standard symbols
    category = unicodedata.category(ch)
    if category.startswith("S") or category.startswith("P") or "EMOJI" in unicodedata.name(ch, ""):
        return True
    
    print("Illigal character detected and suppressed")
    return False


def strip_invalid_utf8(data: bytes) -> str:
    """Strip invalid UTF-8 characters from byte data"""
    # Step 1: decode as much as possible in one go
    text = data.decode("utf-8", errors="ignore")
    valid_text = ''
    for ch in text:
        if is_allowed_char(ch):
            valid_text += ch
        else:
            cp = ord(ch)
            name = unicodedata.name(ch, "<unknown>")
            print(f"[ERROR] Invalid character: '{ch}' (U+{cp:04X}, {name})")
    return valid_text


def try_repair_json(text: str) -> dict:
    """Try to repair malformed JSON by removing invalid characters"""
    for i in range(len(text)):
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            pos = e.pos if hasattr(e, 'pos') else i
            if pos >= len(text):
                break
            text = text[:pos] + text[pos+1:]
    return {
        "raw_text": text,
        "error": "invalid_json_repair_failed"
    }


class UDPHandler:
    def __init__(self, listen_port, target_host, target_port, message_callback=None, message_router=None):
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.target_address = (target_host, target_port)
        self.message_callback = message_callback
        self.message_router = message_router
        
        self.listen_socket = None
        self._running = False
        self._listen_task = None
        
    async def start_listening(self):
        if self._running:
            print("UDP listener already running")
            return
            
        self.listen_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.listen_socket.bind(("", self.listen_port))
        self.listen_socket.setblocking(False)
        
        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())
        #print(f"UDP listener started on port {self.listen_port}")
        
    async def stop_listening(self):
        if not self._running:
            return
            
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
                
        if self.listen_socket:
            self.listen_socket.close()
            self.listen_socket = None
            
        print("UDP listener stopped")
        
    async def _listen_loop(self):
        loop = asyncio.get_running_loop()
        try:
            while self._running:
                data, addr = await loop.sock_recvfrom(self.listen_socket, 1024)
                await self._process_received_message(data, addr)
                
        #except asyncio.CancelledError:
        #    print("UDP listener shutting down")

        except Exception as e:
            print(f"Error in UDP listener: {e}")

        finally:
            if self.listen_socket:
                self.listen_socket.close()
                
    async def _process_received_message(self, data, addr):
        text = strip_invalid_utf8(data)
        message = try_repair_json(text)

        if not message or "msg" not in message:
            print(f"No msg object found in JSON: {message}")
            return

        message["timestamp"] = int(time.time() * 1000)
        #dt = datetime.fromtimestamp(message['timestamp']/1000)
        #readable = dt.strftime("%d %b %Y %H:%M:%S")
        #message["from"] = addr[0]

        if isinstance(message, dict) and isinstance(message.get("msg"), str):
            if self.message_callback:
                await self.message_callback(message)

            if self.message_router:
                await self.message_router.publish('udp', 'mesh_message', message)
                
            #if has_console:
            #    print(f"{readable} {message['src_type']} von {addr[0]}: {message}")

    async def send_message(self, message_data):
        try:
            udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            loop = asyncio.get_running_loop()
            
            json_data = json.dumps(message_data).encode("utf-8")
            await loop.run_in_executor(None, udp_sock.sendto, json_data, self.target_address)
            
            #if has_console:
            #    print(f"UDP message sent to {self.target_address}: {message_data}")
                
        except Exception as e:
            print(f"Error sending UDP message: {e}")
        finally:
            udp_sock.close()
            
    def is_running(self):
        return self._running
