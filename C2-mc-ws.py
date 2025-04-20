import asyncio
import json
import websockets
import socket
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from collections import deque

UDP_PORT_send = 1799
UDP_PORT_list = 1799
#UDP_TARGET = ("dk5en-99.local", UDP_PORT_send)
UDP_TARGET = ("44.149.17.56", UDP_PORT_send)
WS_HOST = "0.0.0.0"
WS_PORT = 2980

#7x24 = 168h
PRUNE_HOURS = 168  # Nachrichten, die älter sind als diese Anzahl Stunden, werden entfernt
MAX_STORE_SIZE_MB = 50

clients = set()
message_store = deque()
message_store_size = 0
store_file_name = "mcdump.json"

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
        #data, addr = await loop.run_in_executor(None, udp_sock.recvfrom, 1024) #alt
        data, addr = await loop.sock_recvfrom(udp_sock, 1024) #neu

        text = strip_invalid_utf8(data)
        message = try_repair_json(text)
        message["timestamp"] = int(time.time() * 1000)
        dt = datetime.fromtimestamp(message['timestamp']/1000)
        readabel = dt.strftime("%d %b %Y %H:%M:%S")

        if isinstance(message, dict) and isinstance(message.get("msg"), str):
            if message["msg"].startswith("{CET}"):
                print(f"{readabel} {message['src_type']} von {addr} Zeit: {message['msg']} ID:{message['msg_id']} src:{message['src']}")
            else:
                print(f"{readabel} {message['src_type']} von {addr}: {message}")
                store_message(message, json.dumps(message)) #wir wollen mit Timestamp speichern

        if clients:
            await asyncio.gather(*[client.send(json.dumps(message)) for client in clients])
    except asyncio.CancelledError: #neu
        print("udp_listener was cancelled. Closing socket.") #neu
    finally: #neu
        udp_sock.close()  #neu

async def websocket_handler(websocket):
    peer = websocket.remote_address[0] if websocket.remote_address else "Unbekannt"
    print(f"WebSocket verbunden von IP {peer}")
    clients.add(websocket)
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
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
        #for item in message_store:
        #     await websocket.send(item["raw"])

        #------------------------------------------------------------------
        # Step 1: Wrap the message list in a structured JSON object
        raw_list = [item["raw"] for item in message_store]
        #raw_list = [
        #    item["raw"].decode("utf-8", errors="replace")
        #    if isinstance(item["raw"], bytes)
        #    else item["raw"]
        #    for item in message_store
        #]

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
    print(f"Nach Message clean {len(message_store)} Nachrichten")

def load_dump():
    global message_store, message_store_size
    if os.path.exists(store_file_name):
        with open(store_file_name, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            message_store = deque(loaded)
            message_store_size = sum(len(json.dumps(m).encode("utf-8")) for m in message_store)
            print(f"Dump geladen: {len(message_store)} Nachrichten ({message_store_size / 1024:.2f} KB)")

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

    print(f"WebSocket-Server läuft auf ws://{WS_HOST}:{WS_PORT}")
    print(f"UDP-Proxy läuft auf Port {UDP_PORT_list}, Weiterleitung an {UDP_TARGET}")
    print("Drücke 'q' + Enter zum Beenden und Speichern")

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def stdin_reader():
        while True:
            line = sys.stdin.readline()
            if not line:
                continue
            if line.strip() == "q":
                loop.call_soon_threadsafe(stop_event.set)
                break

    loop.run_in_executor(None, stdin_reader)
    await stop_event.wait()

    with open(store_file_name, "w", encoding="utf-8") as f:
        json.dump(list(message_store), f, ensure_ascii=False, indent=2)
    print("Beendet und gespeichert.")

    udp_task.cancel()
    print("nach cancel.")
    #ws_server.close(udp_task, return_exception=True)
    ws_server.close()

    print("warten auf close.")
    await ws_server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Manuell beendet mit Ctrl+C")

