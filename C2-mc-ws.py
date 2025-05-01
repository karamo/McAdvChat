import asyncio
import json
import websockets
import socket
import os
import signal
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from collections import deque

VERSION="v0.7.0"
CONFIG_FILE = "/etc/mcadvchat/config.json"

clients = set()
message_store = deque()
message_store_size = 0
has_console = sys.stdout.isatty()

def load_config(path=CONFIG_FILE):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg

def hours_to_dd_hhmm(hours: int) -> str:
    days = hours // 24
    remainder_hours = hours % 24
    return f"{days:02d} day(s) {remainder_hours:02d}:00h"

config = load_config()

UDP_PORT_list = config["UDP_PORT_list"]
print(f"Listening UDP Port: {UDP_PORT_list}")

UDP_PORT_send = config["UDP_PORT_send"]
UDP_TARGET = (config["UDP_TARGET"], UDP_PORT_send)
print(f"MeshCom Target {UDP_TARGET}")

WS_HOST = config["WS_HOST"]
WS_PORT = config["WS_PORT"]
print(f"Websockets Host and Port {WS_HOST}:{WS_PORT}")

PRUNE_HOURS = config["PRUNE_HOURS"]
print(f"Messages older than {hours_to_dd_hhmm(PRUNE_HOURS)} get deleted")

MAX_STORE_SIZE_MB = config["MAX_STORAGE_SIZE_MB"]
print(f"If we get flooded with messages, we drop after {MAX_STORE_SIZE_MB}MB")

store_file_name = config["STORE_FILE_NAME"]
print(f"Messages will be stored on exit: {store_file_name}")


def is_allowed_char(ch: str) -> bool:
    codepoint = ord(ch)

    # Explicit whitelist German Umlaut
    if ch in "äöüÄÖÜß":
        return True
    
    # ASCII 0x20 to 0x5C inclusive
    if 0x20 <= codepoint <= 0x5C:
        return True
    
    # Allow up to 0x7E?
    if 0x5D <= codepoint <= 0x7E:
        return True

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
    
    return False


def get_current_timestamp() -> str:
    return datetime.utcnow().isoformat()

def store_message(message: dict, raw: str):
    global message_store_size
    timestamped = {
        "timestamp": get_current_timestamp(),
        "raw": raw
    }
    message_size = len(json.dumps(timestamped).encode("utf-8"))
    message_store.append(timestamped)
    message_store_size += message_size
    while message_store_size > MAX_STORE_SIZE_MB * 1024 * 1024:
        removed = message_store.popleft()
        message_store_size -= len(json.dumps(removed).encode("utf-8"))

async def udp_listener():
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.bind(("", UDP_PORT_list))
    udp_sock.setblocking(False)  # neu

    loop = asyncio.get_running_loop()
    try: #neu
      while True:
        data, addr = await loop.sock_recvfrom(udp_sock, 1024)

        text = strip_invalid_utf8(data)

        message = try_repair_json(text)
        if not message or "msg" not in message:
           print(f"no msg object found in json: {message}")
       
        msg = message["msg"]
        for c in msg:
          if not is_allowed_char(c):
            cp = ord(c)
            name = unicodedata.name(c, "<unknown>")
            print(f"[ERROR] Invalid character in msg: '{c}' (U+{cp:04X}, {name})")
            print(f"found not allowed character in: {message}")
            message["msg"] = "-- invalid message suppressed --" #we remove bullshit

        message["timestamp"] = int(time.time() * 1000)
        dt = datetime.fromtimestamp(message['timestamp']/1000)
        readabel = dt.strftime("%d %b %Y %H:%M:%S")

        if isinstance(message, dict) and isinstance(message.get("msg"), str):
            if message["msg"].startswith("{CET}"):
                if has_console:
                   print(f"{readabel} {message['src_type']} von {addr} Zeit: {message['msg']} ID:{message['msg_id']} src:{message['src']}")
            else:
                store_message(message, json.dumps(message)) #wir wollen mit Timestamp speichern
                if has_console:
                   print(f"{readabel} {message['src_type']} von {addr}: {message}")

        if clients:
            await asyncio.gather(*[client.send(json.dumps(message)) for client in clients])
    except asyncio.CancelledError: 
        print("udp_listener was cancelled. Closing socket.")
    finally:
        udp_sock.close()

async def websocket_handler(websocket):
    peer = websocket.remote_address[0] if websocket.remote_address else "Unbekannt"
    print(f"WebSocket verbunden von IP {peer}")
    clients.add(websocket)
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                if has_console:
                   print(f"WebSocket empfangen: {data}")
                if data.get("type") == "command":
                   await handle_command(data.get("msg"), websocket)
                else:
                   udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                   udp_sock.sendto(json.dumps(data).encode("utf-8"), UDP_TARGET)
            except json.JSONDecodeError:
                print(f"Fehler: Ungültiges JSON über WebSocket empfangen: {message}")
    except websockets.exceptions.ConnectionClosed as e:
        print(f"WebSocket getrennt von {peer}, Grund: {e.code} - {e.reason}")
    finally:
        print(f"WebSocket getrennt von IP {peer}")
        clients.remove(websocket)

async def handle_command(msg, websocket):
    if msg == "send message dump" or msg == "send pos dump":
        raw_list = [item["raw"] for item in message_store]

        payload = {
            "type": "response",
            "msg": "message dump",
            "data": raw_list 
        }

        # Step 2: Serialize to JSON
        json_data = json.dumps(payload)

        await websocket.send(json_data)
        #------------------------------------------------------------------

        # Step 3: GZIP-compress
        #compressed_data = b"GZ" + gzip.compress(json_data.encode("utf-8"))
        #await websocket.send(compressed_data)

    elif msg == "dump to fs":
        with open(store_file_name, "w", encoding="utf-8") as f:
            json.dump(list(message_store), f, ensure_ascii=False, indent=2)
        print("Daten gespeichert in mcdump.json")

def prune_messages():
    global message_store_size
    cutoff = datetime.utcnow() - timedelta(hours=PRUNE_HOURS)
    temp_store = deque()
    new_size = 0
    for item in message_store:
        if datetime.fromisoformat(item["timestamp"]) > cutoff:
            temp_store.append(item)
            new_size += len(json.dumps(item).encode("utf-8"))
    message_store.clear()
    message_store.extend(temp_store)
    message_store_size = new_size
    #print(f"Nach Message clean {len(message_store)} Nachrichten")

def load_dump():
    global message_store, message_store_size
    if os.path.exists(store_file_name):
        with open(store_file_name, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            message_store = deque(loaded)
            message_store_size = sum(len(json.dumps(m).encode("utf-8")) for m in message_store)
            print(f"{len(message_store)} Nachrichten ({message_store_size / 1024:.2f} KB) geladen")

# UTF-8 Fixer
def strip_invalid_utf8(data: bytes) -> str:
    valid_text = ''
    i = 0
    while i < len(data):
        try:
            char = data[i:i+1]
            char = char.decode("utf-8")
            valid_text += char
            i += 1
        except UnicodeDecodeError:
            i += 1
    return data.decode("utf-8", errors="ignore")

# JSON Repair
def try_repair_json(text: str) -> dict:
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

async def main():

    load_dump()
    prune_messages()

    ws_server = await websockets.serve(websocket_handler, WS_HOST, WS_PORT)
    udp_task = asyncio.create_task(udp_listener())

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

    # 🛡️ Signal-Handler (SIGINT = Ctrl+C, SIGTERM = systemctl stop)
    def handle_shutdown():
       print("Signal empfangen, beende Dienst ...")
       loop.call_soon_threadsafe(stop_event.set)

    # ✅ Signal-Handler registrieren
    for sig in (signal.SIGINT, signal.SIGTERM):
       loop.add_signal_handler(sig, handle_shutdown)

    # 🖥️ Nur wenn interaktives Terminal vorhanden, stdin überwachen
    if sys.stdin.isatty():
       print("Drücke 'q' + Enter zum Beenden und Speichern")
       loop.run_in_executor(None, stdin_reader)
    #else:
    #   print("Kein Terminal erkannt – Eingabe von 'q' deaktiviert")

    print(f"WebSocket ws://{WS_HOST}:{WS_PORT}")
    print(f"UDP-Proxy {UDP_PORT_list}, MeshCom {UDP_TARGET}")

    await stop_event.wait()

    print("Beende Server, speichere Daten …")

    with open(store_file_name, "w", encoding="utf-8") as f:
        json.dump(list(message_store), f, ensure_ascii=False, indent=2)
    print("Beendet und gespeichert.")

    udp_task.cancel()
    print("nach cancel.")
    ws_server.close()

    print("warten auf close.")
    await ws_server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Manuell beendet mit Ctrl+C")

