#!/usr/bin/env python3
import asyncio
import errno
import json
import os
import re
import signal
import socket
import sys
import time
import unicodedata
import websockets
from struct import *
from datetime import datetime, timedelta
from collections import deque, defaultdict
from statistics import mean
from operator import itemgetter

from dbus_next import Variant, MessageType
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next.errors import DBusError, InterfaceNotFoundError
from dbus_next.service import ServiceInterface, method

VERSION="v0.27.0"
CONFIG_FILE = "/etc/mcadvchat/config.json"
if os.getenv("MCADVCHAT_ENV") == "dev":
   print("*** Debug 🐛 and 🔧 DEV Environment detected ***")
   CONFIG_FILE = "/etc/mcadvchat/config.dev.json"

BLUEZ_SERVICE_NAME = "org.bluez"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
DEVICE_INTERFACE = "org.bluez.Device1"
GATT_CHARACTERISTIC_INTERFACE = "org.bluez.GattCharacteristic1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"

AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_PATH = "/com/example/agent"

#read_char_uuid = "6e400003-b5a3-f393-e0a9-e50e24dcca9e" # UUID_Char_NOTIFY
#write_char_uuid = "6e400002-b5a3-f393-e0a9-e50e24dcca9e" # UUID_Char_WRITE
#hello_byte = bytes([0x04, 0x10, 0x20, 0x30])

clients = set()
clients_lock = asyncio.Lock()

message_store = deque()
message_store_size = 0

def load_config(path=CONFIG_FILE):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg

def hours_to_dd_hhmm(hours: int) -> str:
    days = hours // 24
    remainder_hours = hours % 24
    return f"{days:02d} day(s) {remainder_hours:02d}:00h"

def get_current_timestamp() -> str:
    return datetime.utcnow().isoformat()

def is_allowed_char(ch: str) -> bool:
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
    
    print("ende false");
    return False

def strip_invalid_utf8(data: bytes) -> str:
    # Step 1: decode as much as possible in one go
    text = data.decode("utf-8", errors="ignore")  # or "ignore" if you want silent drop
    valid_text = ''
    for ch in text:
        if is_allowed_char(ch):
            valid_text += ch
        else:
            cp = ord(ch)
            name = unicodedata.name(ch, "<unknown>")
            print(f"[ERROR] Invalid character: '{ch}' (U+{cp:04X}, {name})")
    return valid_text

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


def store_message(message: dict, raw: str):
    global message_store_size

    if not isinstance(message, dict):
        if has_console:
            print("store_message: invalid input, message is None or not a dict")
        return

    timestamped = {
        "timestamp": get_current_timestamp(),
        "raw": raw
    }

    if message.get("msg", "<no msg>").startswith("{CET}"):
       if has_console:
         print(message.get("msg", "<no msg>"))
       return

    if message.get("src_type", "<no type>") == "BLE":
       return

    if message.get("src", "<no type>") == "response":
       return

    if message.get("msg", "") == "-- invalid character --":
       return

    if "No core dump" in message.get("msg", ""):
       return

    message_size = len(json.dumps(timestamped).encode("utf-8"))
    message_store.append(timestamped)
    message_store_size += message_size
    while message_store_size > MAX_STORE_SIZE_MB * 1024 * 1024:
        removed = message_store.popleft()
        message_store_size -= len(json.dumps(removed).encode("utf-8"))

async def udp_listener():
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.bind(("", UDP_PORT_list))
    udp_sock.setblocking(False)

    loop = asyncio.get_running_loop()
    try: 
      while True:
        data, addr = await loop.sock_recvfrom(udp_sock, 1024)

        text = strip_invalid_utf8(data)

        message = try_repair_json(text)

        if not message or "msg" not in message:
           print(f"no msg object found in json: {message}")
       
        message["timestamp"] = int(time.time() * 1000)
        dt = datetime.fromtimestamp(message['timestamp']/1000)
        readabel = dt.strftime("%d %b %Y %H:%M:%S")

        message["from"] = addr[0]

        if isinstance(message, dict) and isinstance(message.get("msg"), str):
            await loop.run_in_executor(None, store_message, message, json.dumps(message))
                    
            if has_console:
               print(f"{readabel} {message['src_type']} von {addr[0]}: {message}")

        async with clients_lock:
                targets = list(clients)

        if targets:
            send_tasks = [asyncio.create_task(client.send(json.dumps(message))) for client in targets]
            await asyncio.gather(*send_tasks, return_exceptions=True)

    except asyncio.CancelledError: 
        print("udp_listener was cancelled. Closing socket.")
    finally:
        udp_sock.close()

async def websocket_handler(websocket):
    peer = websocket.remote_address[0] if websocket.remote_address else "unbekannt"
    print(f"WebSocket verbunden von IP {peer}")
    async with clients_lock:
      clients.add(websocket)

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                if has_console:
                   print(f"WebSocket empfangen: {data}")

                if data.get("type") == "command":
                   await handle_command(data.get("msg"), websocket, data.get("MAC"), data.get("BLE_Pin"))

                elif data.get("type") == "BLE":
                   await client.send_message(data.get("msg"), data.get("dst"))

                else:
                   udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                   loop = asyncio.get_running_loop()
                   await loop.run_in_executor(None, udp_sock.sendto,
                                              json.dumps(data).encode("utf-8"),
                                              UDP_TARGET)

            except json.JSONDecodeError:
                print(f"Fehler: Ungültiges JSON über WebSocket empfangen: {message}")

    except websockets.exceptions.ConnectionClosed as e:
        print(f"WebSocket getrennt von {peer}, Grund: {e.code} - {e.reason}")

    finally:
        print(f"WebSocket getrennt von IP {peer}")
        async with clients_lock:
          clients.remove(websocket)

# Constants
BUCKET_SECONDS = 5 * 60
VALID_RSSI_RANGE = (-140, -30)
VALID_SNR_RANGE = (-30, 12)

def is_valid_value(value, min_val, max_val):
    return isinstance(value, (int, float)) and min_val <= value <= max_val

def floor_to_bucket(unix_ms):
    return int(unix_ms // 1000 // BUCKET_SECONDS * BUCKET_SECONDS)

def process_message_store(message_store):
    buckets = defaultdict(lambda: {"rssi": [], "snr": []})  # key: (bucket_time, callsign)

    for item in message_store:
        raw_str = item.get("raw")
    
        if not raw_str:
            print("not str")
            continue
        try:
            parsed = json.loads(raw_str)
        except json.JSONDecodeError:
            continue

        #src = parsed.get("src")
        src = safe_get(parsed, "src")

        
        if not src:
            continue

        callsigns = [s.strip() for s in src.split(",")]

        rssi = parsed.get("rssi")
        snr = parsed.get("snr")

        timestamp_ms = parsed.get("timestamp")
        if timestamp_ms is None:
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

        avg_rssi = round(mean(rssi_values), 1)
        avg_snr = round(mean(snr_values), 1)
        result.append({
            "src_type": "STATS",
            "timestamp": bucket_time,
            "callsign": callsign,
            "rssi": avg_rssi,
            "snr": avg_snr,
            "count": count
        })

    return result

async def dump_mheard_data(websocket):
    mheard = process_message_store(message_store)

    payload = {
            "type": "response",
            "msg": "mheard stats",
            "data": mheard
        }

    json_data = json.dumps(payload)
    await websocket.send(json_data)

def get_initial_payload():
    recent_items = list(reversed(message_store))
    pos_msgs = [i["raw"] for i in recent_items if '"type": "pos"' in i["raw"]][:100]
    msg_msgs = [i["raw"] for i in recent_items if '"type": "msg"' in i["raw"]][:200]
    ack_msgs = [i["raw"] for i in recent_items if '"type": "ack"' in i["raw"]][:200]

    return list(reversed(msg_msgs)) + list(reversed(ack_msgs)) + pos_msgs

def get_full_dump():
    pos_items = list(reversed([item for item in message_store if item.get("type") == "pos"]))
    other_items = [item for item in message_store if item.get("type") != "pos"]

    # Combine them: sorted 'pos' first, then the rest (unchanged order)
    final_items = other_items + pos_items
    return [item["raw"] for item in final_items]

async def handle_command(msg, websocket, MAC, BLE_Pin):
    if msg == "send message dump" or msg == "send pos dump":

        preview = {
          "type": "response",
          "msg": "message dump",
          "data": get_initial_payload()
        }
        await websocket.send(json.dumps(preview))
        await asyncio.sleep(1)

        CHUNK_SIZE = 1000

        full_data = get_full_dump()
        total = len(full_data)

        for i in range(0, total, CHUNK_SIZE):
            if has_console:
               print("sending message chunk ",i)
            chunk = full_data[i:i+CHUNK_SIZE]
            full = {
                "type": "response",
                "msg": "message dump",
                "data": chunk
            }
            await websocket.send(json.dumps(full))
            await asyncio.sleep(0)  # yield to event loop without delay

        #------------------------------------------------------------------

        # Step 3: GZIP-compress
        #compressed_data = b"GZ" + gzip.compress(json_data.encode("utf-8"))
        #await websocket.send(compressed_data)

    elif msg == "mheard dump":
        await dump_mheard_data(websocket)

    elif msg == "dump to fs":
        with open(store_file_name, "w", encoding="utf-8") as f:
            json.dump(list(message_store), f, ensure_ascii=False, indent=2)
        print("Daten gespeichert in mcdump.json")

    elif msg == "scan BLE":
        await scan_ble_devices()

    elif msg == "BLE info":
        await ble_info()

    elif msg == "pair BLE":
        await ble_pair(MAC, BLE_Pin)

    elif msg == "unpair BLE":
        await ble_unpair(MAC)

    elif msg == "disconnect BLE":
        await ble_disconnect()

    elif msg == "connect BLE":
        #print("line 283", MAC)
        await ble_connect(MAC)

    elif msg == "resolve-ip":
        await backend_resolve_ip(MAC)

    elif msg.startswith("--setboostedgain"):
        if client is not None:
           await client.a0_commands(msg)
        else:
           await blueZ_bubble('a0_command result', 'error', "client not connected" )

    elif (msg.startswith("--set") | msg.startswith("--sym")):
        if client is not None:
           await client.set_commands(msg)

    elif msg.startswith("--"):
        if client is not None:
           await client.a0_commands(msg)
        else:
           await blueZ_bubble('a0_command result', 'error', "client not connected" )

    else:
        print(f"command not available", msg)

async def blueZ_bubble(command, result, msg):
      message={ 'src_type': 'BLE', 
                'TYP': 'blueZ', 
                'command': command,
                'result': result,
                'msg': msg,
                "timestamp": int(time.time() * 1000)
              }
      await ws_send(message)

async def backend_resolve_ip(hostname):
    if has_console:
       print("resolving ip", hostname)

    loop = asyncio.get_event_loop()

    try:
        infos = await loop.run_in_executor(None, socket.getaddrinfo, hostname, None)
        ip = infos[0][4][0]
        if has_console:
           print(f"Resolved IP: {ip}")
        
        await blueZ_bubble("resolve-ip", "ok", ip)
    except Exception as e:
        if has_console:
           print(f"Error resolving IP: {e}")
        await blueZ_bubble("resolve-ip", "error", str(e)) 


def mac_to_dbus_path(mac):
    return f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

async def find_gatt_characteristic(bus, path, target_uuid):
     try:
        introspect = await bus.introspect(BLUEZ_SERVICE_NAME, path)
     except Exception as e:
        return None, None

     for node in introspect.nodes:
        child_path = f"{path}/{node.name}"
        try:
            child_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, child_path, await bus.introspect(BLUEZ_SERVICE_NAME, child_path))

            props_iface = child_obj.get_interface(PROPERTIES_INTERFACE)

            props = await props_iface.call_get_all(GATT_CHARACTERISTIC_INTERFACE)

            uuid = props.get("UUID").value.lower()
            if uuid == target_uuid.lower():
                char_iface = child_obj.get_interface(GATT_CHARACTERISTIC_INTERFACE)
                return child_obj, char_iface

        except Exception:
            # Falls keine Properties oder keine GattCharacteristic1, rekursiv weitersuchen
            obj, iface = await find_gatt_characteristic(bus, child_path, target_uuid)
            if iface:
                return obj, iface  # ❗beides weitergeben

     return None, None

class BLEClient:
    def __init__(self, mac, read_uuid, write_uuid, hello_bytes=None):
        self.mac = mac
        self.read_uuid = read_uuid
        self.write_uuid = write_uuid
        self.hello_bytes = hello_bytes or b'\x00'
        self.path = mac_to_dbus_path(mac)
        self.bus = None
        self.device_obj = None
        self.dev_iface = None
        self.read_char_iface = None
        self.read_props_iface = None
        self.write_char_iface = None
        self.props_iface = None
        self._on_value_change_cb = None
        self._connect_lock = asyncio.Lock()
        self._connected = False

    async def connect(self):
      async with self._connect_lock:
        if self._connected:
             if has_console:
                print(f"🔁 Verbindung zu {self.mac} besteht bereits")
             return

        if self.bus is None:
           self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        introspection = await self.bus.introspect(BLUEZ_SERVICE_NAME, self.path)
        self.device_obj = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, self.path, introspection)
        try:
           self.dev_iface = self.device_obj.get_interface(DEVICE_INTERFACE)
        except InterfaceNotFoundError as e:
           print(f"⚠️ Interface not found, device not paired: {e}")
           await blueZ_bubble('connect BLE result','error', "Interface not found, device not paired")

           self._connected = False
           self.bus = None
           return

        self.props_iface = self.device_obj.get_interface(PROPERTIES_INTERFACE)

        try:
           connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        except DBusError as e:
           if has_console:
             print(f"⚠️ Fehler beim Abfragen des Verbindungsstatus: {e}")
           await blueZ_bubble('connect BLE result','error',f"⚠️  Error checkin link state: {e}")
           self._connected = False
           return

        if not connected:
           try:
             await self.dev_iface.call_connect()
             if has_console:
                print(f"✅ verbunden mit {self.mac}")

           except DBusError as e:
             self.bus = None
             if has_console:
                print(f"⚠️  Connect timeout: {e}")
             await blueZ_bubble('connect BLE result','error',f"⚠️  Connect timeout: {e}")
             return

        else:
           if has_console:
              print(f"🔁 Verbindung zu {self.mac} besteht bereits")

        await self._find_characteristics()

        if not self.read_char_iface or not self.write_char_iface:
            print("❌ Charakteristika nicht gefunden")
            #msg={ 'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'connect', 'result': 'error', 'msg': "connection not established, not yet paired" }
            #await ws_send(msg)
            await blueZ_bubble('connect BLE result','error', "❌ connection not established, not yet paired")

            self._connected = False
            self.bus = None
            return
        
        self.read_props_iface = self.read_char_obj.get_interface(PROPERTIES_INTERFACE)

        try:
          is_notifying = (await self.read_props_iface.call_get(GATT_CHARACTERISTIC_INTERFACE, "Notifying")).value
          if has_console:
             print("Notifications sind .. ", is_notifying)
        except DBusError as e:
             print(f"⚠️ Fehler beim Abfragen von Notifying: {e}")

        self._connected = True
        #msg={ 'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'connect BLE result', 'result': 'ok' }
        #await ws_send(msg)
        await blueZ_bubble('connect BLE result','ok', "connection established, downloading config ..")
        #print("zeile 390",msg) 


    async def _find_characteristics(self):
        self.read_char_obj, self.read_char_iface = await find_gatt_characteristic(
            self.bus, self.path, self.read_uuid)
        self.write_char_obj, self.write_char_iface = await find_gatt_characteristic(
            self.bus, self.path, self.write_uuid)


    async def start_notify(self, on_change=None):
        if not self._connected: 
           await blueZ_bubble('notify','error', f"❌ connection not established")
           if has_console:
              print("❌ Connection not established, start notify aborted")
           return

        if has_console:
           print("▶️  Start notify ..")


        is_notifying = (await self.read_props_iface.call_get(GATT_CHARACTERISTIC_INTERFACE, "Notifying")).value
        if is_notifying:
           if has_console:
              print("wir haben schon ein notify, also nix wie weg hier")
           return


        if not self.bus:
           print("❌ Connection not established, start notify aborted")
           await blueZ_bubble('notify','error', f"❌ connection not established")
           return

        if not self.read_char_iface:
            raise Exception("read_char_iface nicht initialisiert")

        try:
            if on_change:
                self._on_value_change_cb = on_change

            self.read_props_iface.on_properties_changed(self._on_props_changed)
            await self.read_char_iface.call_start_notify()

            is_notifying = (await self.read_props_iface.call_get(GATT_CHARACTERISTIC_INTERFACE, "Notifying")).value

            if has_console:
               print(f"📡 Notify gestartet, Status: {is_notifying}")
        except DBusError as e:
            print(f"⚠️ StartNotify fehlgeschlagen: {e}")

    async def _on_props_changed(self, iface, changed, invalidated):
      if iface != GATT_CHARACTERISTIC_INTERFACE:
        return

      if "Value" in changed:
        new_value = changed["Value"].value
        await notification_handler(new_value)

        if self._on_value_change_cb:
            self._on_value_change_cb(new_value)

    async def stop_notify(self):
        if not self.bus:
           print("🛑 connection not established, can't stop notify ..")
           await blueZ_bubble('notify','error', f"❌ connection not established")
           return

        if not self.read_char_iface:
           print("🛑 no read interface, can't stop notify ..")
           await blueZ_bubble('notify','error', f"❌ no read interface, can't stop notify")
           return
        try:
            await self.read_char_iface.call_stop_notify()
            print("🛑 Notify gestoppt")
            await blueZ_bubble('disconnect','info', "unsubscribe from messages ..")

        except DBusError as e:
            if "No notify session started" in str(e):
                if has_console:
                   print("ℹ️ Keine Notify-Session – ignoriert")
            else:
                raise

    async def send_hello(self):
        if not self.bus:
           print("🛑 connection not established, can't send hello ..")
           #msg={ 'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'send_hello result', 'result': 'error', 'msg': "connection not established, can't send" }
           #await ws_send(msg)
           await blueZ_bubble('send hello','error', f"❌ connection not established")
           return

        connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        if not connected:
           print("🛑 connection lost, can't send ..")
           #msg={ 'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'send_hello result', 'result': 'error', 'msg': "connection lost, can't send" }
           await blueZ_bubble('send hello','error', f"❌ connection lost")

           await self.disconnect() #aufräumen, vielleicht hilft es etwas
           await self.close() #aufräumen, vielleicht hilft es etwas
           return

        if self.write_char_iface:
            await self.write_char_iface.call_write_value(self.hello_bytes, {})
            await blueZ_bubble('conf load','info', ".. waking up device ..")
            if has_console:
               print(f"📨 Hello sent ..")

        else:
            print("⚠️ Keine Write-Charakteristik verfügbar")

    async def send_message(self, msg, grp):
        if not self.bus:
           print("🛑 connection not established, can't send ..")
           #msg={ 'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'send_message result', 'result': 'error', 'msg': "connection not established, can't send" }
           #await ws_send(msg)
           await blueZ_bubble('send message','error', f"❌ connection not established")
           return

        connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        if not connected:
           print("🛑 connection lost, can't send ..")
           #msg={ 'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'send_message result', 'result': 'error', 'msg': "connection lost, can't send" }
           await blueZ_bubble('send message','error', f"❌ connection lost")

           await self.disconnect() #aufräumen, vielleicht hilft es etwas
           await self.close() #aufräumen, vielleicht hilft es etwas
           return

        message = "{" + grp + "}" + msg
        byte_array = bytearray(message.encode('utf-8'))

        laenge = len(byte_array) + 2

        byte_array = laenge.to_bytes(1, 'big') +  bytes ([0xA0]) + byte_array

        if self.write_char_iface:
            try:
              await asyncio.wait_for(self.write_char_iface.call_write_value(byte_array, {}), timeout=5)
              #if has_console:
              #   print(f"📨 Message sent .. {byte_array}")
            except asyncio.TimeoutError:
              print("🕓 Timeout beim Schreiben an BLE-Device")
              await blueZ_bubble('send message','error', f"❌ Timeout on write")
            except Exception as e:
              print(f"💥 Fehler beim Schreiben an BLE: {e}")
              await blueZ_bubble('send message','error', f"❌ BLE write error {e}")
        else:
            print("⚠️ Keine Write-Charakteristik verfügbar")


     #https://github.com/karamo/MeshAll42_MIT-AI2/tree/main/MeshCOM_Interna#12-anforderungspakete-aus-der-app-an-die-fw
     # tested against the following commands
       #--pos
       #--wx
       #--sendpos
       #--reboot
       #--gps
       #--bme280
       #--bmp280
       #--bme680
       #--mesh on/off
       #--display on/off
       #--gateway on/off
    async def a0_commands(self, cmd):
        if not self.bus:
           print("🛑 connection not established, can't send ..")
           await blueZ_bubble('a0 command','error', f"❌ connection not established")
           return

        connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        if not connected:
            await blueZ_bubble('a0 command','error', f"❌ connection lost")
            print("🛑 connection lost, can't send ..")
            await self.disconnect() #aufräumen, vielleicht hilft es etwas
            await self.close() #aufräumen, vielleicht hilft es etwas
            return

        if has_console:
          print(f"✅ ready to send")

        byte_array = bytearray(cmd.encode('utf-8'))

        laenge = len(byte_array) + 2

        byte_array = laenge.to_bytes(1, 'big') +  bytes ([0xA0]) + byte_array

        if self.write_char_iface:
            await self.write_char_iface.call_write_value(byte_array, {})
            if has_console:
               print(f"📨 Message sent .. {byte_array}")

        else:
            print("⚠️ Keine Write-Charakteristik verfügbar")


       #--mheard -> gibts nicht
       #--path -> gibts nichs
    async def set_commands(self, cmd):
       laenge = 0
       print("special commands, not yet implemented")
       
       if not self.bus:
          #msg={ 'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'set_command result', 'result': 'error', 'msg': "connection not established, can't send" }
          #await ws_send(msg)
          await blueZ_bubble('set command','error', f"❌ connection not established")
          print("🛑 connection not established, can't send ..")
          return

       connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
       if not connected:
            print("🛑 connection lost, can't send ..")
            await blueZ_bubble('set command','error', f"❌ connection lost")
            await self.disconnect() #aufräumen, vielleicht hilft es etwas
            if has_console:
               print("debug: disconnect ..")

            await self.close() #aufräumen, vielleicht hilft es etwas
            if has_console:
               print("debug: close ..")
            return

       if has_console:
          print(f"✅ ready to send")

       #ID = 0x20 Timestamp from phone [4B]
       if cmd == "--settime":
         cmd_byte = bytes([0x20])

         time_str=get_current_timestamp()
         time_str=time_str.replace("T", " ").split(".")[0]
         if has_console:
            print(f"Aktuelle Zeit {time_str}")

         byte_array = bytearray(time_str.encode('utf-8'))

         laenge = len(byte_array) + 3
         byte_array = laenge.to_bytes(1, 'big') +  cmd_byte + byte_array + bytes([0x4b])

         if self.write_char_iface:
            await self.write_char_iface.call_write_value(byte_array, {})
            if has_console:
               print(f"📨 Message sent .. {byte_array}")

         else:
            print("⚠️ Keine Write-Charakteristik verfügbar")


       elif cmd == "--setCALL":
         cmd_byte = bytes([0x50])
         #0x50 - Callsign (--setCALL) [1B len - Callsign]

       elif cmd == "--setSSID" or cmd == "--setPWD":
         param="TEST123"
         cmd_byte = bytes([0x55])
         laenge = len(param)
         #0x55 - Wifi SSID (--setSSID) and PWD (--setPWD) [1B - SSID Length - SSID - 1B PWD Length - PWD]

       elif cmd == "--setLAT":
         param="47.123"
         cmd_byte = bytes([0x70])
         #0x70 - Latitude (--setLAT) [1B length + 1B Msg-ID + 4B lat + 1B save_flag]
         laenge = len(param)

       elif cmd == "--setLON":
         param="47.123"
         cmd_byte = bytes([0x80])
         #0x80 - Longitude (--setLON) [1B length + 1B Msg-ID + 4B lon + 1B save_flag]
         laenge = len(param)

       elif cmd == "--setALT":
         cmd_byte = bytes([0x90])
         #0x90 - Altitude (--setALT) [1B length + 1B Msg-ID + 4B alt + 1B save_flag]

       elif cmd == "--symID" or cmd =="--symCD":
         param="G"
         cmd_byte = bytes([0x90])
         #0x95 - APRS Symbols (--symID --symCD)
         laenge=len(param)

       elif cmd == "--setFlash":
       #0xF0 - Save Settings to Flash
         cmd_byte = bytes([0xF0])

       #     save_flag is 0x0A for save and 0x0B for don't save
       #(Aus Z.365ff https://github.com/icssw-org/MeshCom-Firmware/blob/oe1kfr_434q/src/phone_commands.cpp)
       
       if has_console:
          print(f"alles zusammen und raus damit {cmd_byte} {laenge}")

    async def monitor_connection(self):
        if not self.bus:
            print("⚠️ Kein D-Bus verbunden")
            await blueZ_bubble('D-Bus','error', f"❌ kein D-Bus verbunden")
            return

        def handle_properties_changed(message):
            if message.message_type != MessageType.SIGNAL:
                return
            if message.interface != "org.freedesktop.DBus.Properties":
                return
            if message.member != "PropertiesChanged":
                return
            if message.path != self.path:
                return

            interface_name, changed_props, _ = message.body
            if interface_name != DEVICE_INTERFACE:
                return

            if "Connected" in changed_props:
                connected = changed_props["Connected"].value
                if not connected:
                    print(f"📴 Verbindung zu {self.mac} wurde unterbrochen!")
                    blueZ_bubble('Monitoring','error', f"❌ Verbindung zu {self.mac} unterbrochen")
                    self._connected = False
                    self.bus = None
                    # evtl. neu verbinden oder clean-up triggern

        self.bus.add_message_handler(handle_properties_changed)
        if has_console:
           print(f"👂 Überwache BLE-Verbindung zu {self.mac}")


    async def disconnect(self):
        if not self.dev_iface:
            if has_console:
               print("⬇️  not connected - can't disconnect ..")
            return
        try:
            if has_console:
              print("⬇️ disconnect ..")
            await blueZ_bubble('disconnect','info', "disconnecting ..")
            await self.stop_notify()
            await self.dev_iface.call_disconnect()
            await blueZ_bubble('disconnect','ok', "✅ disconnected")
            print(f"🧹 Disconnected von {self.mac}")

        except DBusError as e:
            await blueZ_bubble('disconnect','error', f"❌ disconnect error {e}")
            if has_console:
               print(f"⚠️ Disconnect fehlgeschlagen: {e}")


    async def close(self):
        if self.bus:
            self.bus.disconnect()
        self.bus = None
        self._connected = False

    def _normalize_variant(self,value):
      if isinstance(value, Variant):
        return self._normalize_variant(value.value)
      elif isinstance(value, dict):
        return {k: self._normalize_variant(v) for k, v in value.items()}
      elif isinstance(value, list):
        return [self._normalize_variant(v) for v in value]
      elif isinstance(value, bytes):
        return value.hex()
      else:
        return value

    async def ble_info(self):
      if not self.props_iface:
          if has_console:
            print("⚠️  not connected, can't ask for info")
          #msg={ 'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'BLE info result', 'result': 'error', 'msg': "not connected, can't ask for info"}
          #await ws_send(msg)
          await blueZ_bubble('BLE info result','error','not connected')
          return

      try:
        props = await self.props_iface.call_get_all(DEVICE_INTERFACE)

        if has_console:
           print("🔍 BLE Device Info:")

        for key, val in props.items():
            print(f"  {key}: {val.value}")

        normalized= self._normalize_variant(props)
        normalized["TYP"] = "blueZinfo"
        msg=transform_ble(normalized)
        await ws_send(msg)

      except Exception as e:
        print(f"❌ Failed to fetch info for {self.props_iface}: {e}")


    async def scan_ble_devices(self, timeout=5.0):
      #Helper function
      async def _interfaces_added(path, interfaces):
        if DEVICE_INTERFACE in interfaces:
            props = interfaces[DEVICE_INTERFACE]
            name = props.get("Name", Variant("s", "")).value
            if name.startswith("MC-"):
              addr = props.get("Address", Variant("s", "")).value
              rssi = props.get("RSSI", Variant("n", 0)).value
              self.found_devices[path] = (name, addr, rssi)
              #print(f"🔹 {name} | Address: {addr} | RSSI: {rssi}", end="\r")

            #if name.startswith("MC-"):
            #    print("✅ Matching device found. Stopping discovery early...")
            #    found_mc_event.set()

      await blueZ_bubble('scan BLE','info','command started')

      if self.bus is None:
          self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
      else:
          print("❌ already connected, no scanning possible ..")
          await blueZ_bubble( 'scan BLE result', 'error', "already connected, no scanning possible")
          return

      if has_console:
         print("🔍 Starting native BLE scan via BlueZ... timout =",timeout)
      await blueZ_bubble('scan BLE','info', ('🔍 BLE scan active... timout =' +  str(timeout)))

      path = "/org/bluez/hci0"

      introspection = await self.bus.introspect(BLUEZ_SERVICE_NAME, path)
      device_obj = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, introspection)
      self.adapter = device_obj.get_interface(ADAPTER_INTERFACE)
     
      # Track discovered devices
      self.found_devices = {}
      # Event zur Synchronisation
      found_mc_event = asyncio.Event()

      # Listen to InterfacesAdded signal # Subscribe to the signal
      self.obj_mgr = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, "/", await self.bus.introspect(BLUEZ_SERVICE_NAME, "/"))
      self.obj_mgr_iface = self.obj_mgr.get_interface(OBJECT_MANAGER_INTERFACE)

      objects = await self.obj_mgr_iface.call_get_managed_objects()

      device_count = 0
      for path, interfaces in objects.items():
        if DEVICE_INTERFACE in interfaces:
          device_count += 1
          props = interfaces[DEVICE_INTERFACE]
          name = props.get("Name", Variant("s", "")).value
          addr = props.get("Address", Variant("s", "")).value
          paired = props.get("Paired", Variant("b", False)).value

          # ➕ New section: busy status
          connected = props.get("Connected", Variant("b", False)).value
          services_resolved = props.get("ServicesResolved", Variant("b", False)).value
          busy = connected or services_resolved  
          # ➕ Add busy flag to your device dictionary for later use (e.g. websocket message)
          interfaces[DEVICE_INTERFACE]["Busy"] = Variant("b", busy)
        
          if has_console:
            #print(f"💾 Found device: {name} ({addr}, {paired})")
            print(f"💾 Found device: {name} ({addr}, paired={paired}, busy={busy})")

      objects["TYP"] = "blueZknown"
      msg=transform_ble(self._normalize_variant(objects))
      await ws_send(msg)

      if has_console:
         print(f"\n✅ Found {device_count} known device(s):")
      await blueZ_bubble('scan BLE','info', f".. found {device_count} known device(s) ..")

      #Handler installieren
      def on_interfaces_added_sync(path, interfaces):
          asyncio.create_task(_interfaces_added(path, interfaces))

      self.obj_mgr_iface.on_interfaces_added(on_interfaces_added_sync)

      # Start discovery
      await self.adapter.call_start_discovery()

      try:
         # Warte bis entweder Gerät gefunden oder Timeout abgelaufen
         await asyncio.wait_for(found_mc_event.wait(), timeout)
      except asyncio.TimeoutError:
         #print(f"⏱ Timeout expired after {timeout:.1f}s, no matching device found.")
         print("\n")

      await self.adapter.call_stop_discovery()

      print(f"\n✅ Scan complete. Not paired {len(self.found_devices)} device(s)")
      await blueZ_bubble('scan BLE','info', f"✅ Scan complete. Not paired {len(self.found_devices)} device(s)")

      for path, (name, addr, rssi) in self.found_devices.items():
          print(f"🔹 {name} | Address: {addr} | RSSI: {rssi}")

      self.found_devices["TYP"] = "blueZunKnown"
      msg=transform_ble(self._normalize_variant(self.found_devices))
      await ws_send(msg)

      await self.close() #sauber aufräumen


class NoInputNoOutputAgent(ServiceInterface):
    def __init__(self):
        super().__init__('org.bluez.Agent1')

    @method()
    def Release(self):
        if has_console:
           print("Agent released")

    @method()
    def RequestPasskey(self, device: 'o') -> 'u':
       print(f"Passkey requested for {device}")
       return 0  # Return the integer passkey 0 (i.e. "000000")

    @method()
    def RequestPinCode(self, device: 'o') -> 's':
        print(f"PIN requested for {device}")
        return "000000"

    @method()
    def DisplayPinCode(self, device: 'o', pincode: 's'):
        print(f"DisplayPinCode for {device}: {pincode}")

    @method()
    def RequestConfirmation(self, device: 'o', passkey: 'u'):
        print(f"Confirm passkey {passkey} for {device}")
        # Auto-confirm
        return

    @method()
    def AuthorizeService(self, device: 'o', uuid: 's'):
        print(f"Authorize service {uuid} for {device}")
        return

    @method()
    def Cancel(self):
        print("Request cancelled")


async def ble_pair(mac, BLE_Pin):
    path = mac_to_dbus_path(mac)
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    # Register agent
    agent = NoInputNoOutputAgent()
    bus.export(AGENT_PATH, agent)

    manager_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, "/org/bluez", await bus.introspect(BLUEZ_SERVICE_NAME, "/org/bluez"))
    agent_manager = manager_obj.get_interface("org.bluez.AgentManager1")
    await agent_manager.call_register_agent(AGENT_PATH, "KeyboardDisplay")

    await agent_manager.call_request_default_agent(AGENT_PATH)

    # Pair device
    dev_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, await bus.introspect(BLUEZ_SERVICE_NAME, path))

    try:
        dev_iface = dev_obj.get_interface(DEVICE_INTERFACE)
    except InterfaceNotFoundError as e:
        print("❌ Error, device not found!")
        #msg={ 'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'BLE pair result', 'result': 'error', 'msg': "device not found", 'mac': mac}
        #await ws_send(msg)
        await blueZ_bubble('BLE pair result','error', f"❌ device not found {mac}: {e}")
        return

    try:
        await dev_iface.call_pair()
        if has_console:
           print(f"✅ Successfully paired with {mac}")

        await dev_iface.set_trusted(True)
        if has_console:
           print(f"🔐 Device {mac} marked as trusted.")

        is_paired = await dev_iface.get_paired()
        if has_console:
           print(f"📎 Paired state of {mac}: {is_paired}")

        is_trusted = await dev_iface.get_trusted()
        if has_console:
           print(f"Trust state: {is_trusted}")

        is_bonded = await dev_iface.get_bonded()
        if has_console:
           print(f"Bond state: {is_bonded}")

        await asyncio.sleep(2)  # allow time for registration to settle
        await blueZ_bubble('ble_pair result', 'ok', f"✅ Successfully paired {mac}" )

        try:
           await dev_iface.call_disconnect()
           print(f"🔌 Disconnected from {mac} after pairing.")
        except Exception as e:
           print(f"⚠️ Could not disconnect from {mac}: {e}")

    except Exception as e:
        print(f"❌ Failed to pair with {mac}: {e}")
        await blueZ_bubble('BLE pair result','error', f"❌ failed to pair {mac}: {e}")

async def ble_unpair(mac):
    if has_console:
       print(f"🧹 Unpairing {mac} using blueZ ...")

    device_path = mac_to_dbus_path(mac)
    adapter_path = "/org/bluez/hci0"

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    # Unpairing logic
    adapter_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, adapter_path,
                                   await bus.introspect(BLUEZ_SERVICE_NAME, adapter_path))
    adapter_iface = adapter_obj.get_interface("org.bluez.Adapter1")

    try:
      await adapter_iface.call_remove_device(device_path)
    except DBusError as e:
      print(f"❌ device {mac}",e)
      #msg={ 'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'BLE unpair result', 'result': 'error', 'mac': mac}
      #await ws_send(msg)
      await blueZ_bubble('BLE unpair result','error', f"❌ device {mac}")
      return
 
    print(f"🧹 Unpaired device {mac}")
    await blueZ_bubble('BLE unpair','ok', f"✅ Unpaired device {mac}")



async def ble_connect(MAC):
    global client  # we are assigning to global

    if client is None:
        client = BLEClient(
            mac=MAC,
            read_uuid="6e400003-b5a3-f393-e0a9-e50e24dcca9e",
            write_uuid="6e400002-b5a3-f393-e0a9-e50e24dcca9e",
            hello_bytes=b'\x04\x10\x20\x30'
        )

    if not client._connected: 
      await client.connect()
      await client.start_notify()
      await client.monitor_connection()
      await client.send_hello()
    else:
      await blueZ_bubble('connect BLE result','info', "BLE connection already running")

      if has_console:
         print("can't connect, already connected")

async def ble_disconnect():
    global client  # we are assigning to global
    if client is None:
      return

    if client._connected: 
      await client.disconnect()
      await client.close()
      client = None
    else:
      await blueZ_bubble('disconnect BLE result','error', "can't disconnect, already discconnected")

      if has_console:
         print("❌ can't disconnect, already disconnected")

async def scan_ble_devices():
    scanclient = BLEClient(
        mac ="",
        read_uuid = "6e400003-b5a3-f393-e0a9-e50e24dcca9e", # UUID_Char_NOTIFY
        write_uuid = "6e400002-b5a3-f393-e0a9-e50e24dcca9e", # UUID_Char_WRITE
        hello_bytes = b'\x04\x10\x20\x30'
    )
    await scanclient.scan_ble_devices()

async def ble_info():
    if client is None:
      await blueZ_bubble('ble_info result', 'error', "client not connected" )
      return

    await client.ble_info()

async def ws_send_json(message):
             output = dispatcher(message)
             if has_console:
               print(json.dumps(output, indent=2))
             await ws_send(output)

async def ws_send(output):
             loop = asyncio.get_running_loop()
             await loop.run_in_executor(None, store_message, output, json.dumps(output))

             #Alles an den WebSocket
             async with clients_lock:
                targets = list(clients)

             if targets:
                send_tasks = [asyncio.create_task(client.send(json.dumps(output))) for client in targets]
                await asyncio.gather(*send_tasks, return_exceptions=True)



async def notification_handler(clean_msg):
    # JSON-Nachrichten beginnen mit 'D{'
    if clean_msg.startswith(b'D{'):

         var = decode_json_message(clean_msg)

         typ_mapping = {
               "MH": "MHead update",
               "SA": "APRS",
               "G": "GPS",
               "W": "weather",
               "SN": "System Settings",
               "SE": "pressure und Co sensors",
               "SW": "Wifi ttings",
               "I": "Info page",
               "IO": "IO page",
               "TM": "TM page",
               "AN": "AN page",
               "CONFFIN": "Habe fertig"
         }

         try:
           typ = var.get('TYP')

           #print("type_map",typ_mapping.get(var.get('TYP'), var))

           if typ == 'MH': # MH update
             #if has_console:
             #  print("MH",var)
             await ws_send_json(var)

           elif typ == "SA": # APRS.fi Info
             #if has_console:
             #  print("APRS", var)
             await ws_send_json(var)

           elif typ == "G": # GPS Info
             #if has_console:
             #  print("GPS", var)
             await ws_send_json(var)

           elif typ == "W": # Wetter Info
             #if has_console:
             #  print("Wetter", var)
             await ws_send_json(var)

           elif typ == "SN": # System Settings 
             #if has_console:
             #  print("System Settings", var)
             await ws_send_json(var)

           elif typ == "SE": # System Settings
             #if has_console:
             #  print("Druck und Co Sensoren",var)
             await ws_send_json(var)

           elif typ == "SW": # WIFI + IP Settings
             #if has_console:
             #  print("Wifi Settings")
             await ws_send_json(var)

           elif typ == "I": # Info Seite
             #if has_console:
             #  print("Info Seite", var)
             await ws_send_json(var)

           elif typ == "IO": # neu
             #if has_console:
             #  print("Info Seite", var)
             await ws_send_json(var)

           elif typ == "TM": # neu
             #if has_console:
             #  print("neu", var)
             await ws_send_json(var)

           elif typ == "AN": # 
             #if has_console:
             #  print("neu", var)
             await ws_send_json(var)

           elif typ == "CONFFIN": # Habe Fertig! Mehr gibt es nicht
             await blueZ_bubble('conffin','ok', "✅ finished sending config")

             if has_console:
                print("Habe fertig",var)
           else:
             if has_console:
                print("type unknown",var)

         except KeyError:
             print(error,var) 

    # Binärnachrichten beginnen mit '@'
    elif clean_msg.startswith(b'@'):
      message = decode_binary_message(clean_msg)
      #if has_console:
      #   print("bin decode", message)
      await ws_send_json(message)

    else:
        print("Unbekannter Nachrichtentyp.")

def calc_fcs(msg):
    fcs = 0
    for x in range(0,len(msg)):
        fcs = fcs + msg[x]
    
    # SWAP MSB/LSB
    fcs = ((fcs & 0xFF00) >> 8) | ((fcs & 0xFF) << 8 )
    
    #print("calc_fcs=" + hex(fcs))
    return fcs

def decode_json_message(byte_msg):
    try:
        json_str = byte_msg.rstrip(b'\x00').decode("utf-8")[1:]
        return json.loads(json_str)

    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"Fehler beim Dekodieren der JSON-Nachricht: {e}")
        return None

def decode_binary_message(byte_msg):
    # little-endian unpack
    raw_header = byte_msg[1:7]
    [payload_type, msg_id, max_hop_raw] = unpack('<BIB', raw_header)

    #Bits schieben
    max_hop = max_hop_raw & 0x0F
    mesh_info = max_hop_raw >> 4

    #Frame checksum berechnen
    calced_fcs = calc_fcs(byte_msg[1:-11])

    remaining_msg = byte_msg[7:].rstrip(b'\x00')  # Alles nach Hop

    if byte_msg[:2] == b'@A':  # Prüfen, ob es sich um ACK Frames handelt

       #remaining_msg = byte_msg[8:].rstrip(b'\x00')  # Alles nach Hop
       message = remaining_msg.hex().upper()

       #Etwas bit banging, weil die Binaerdaten am Ende immer gleich aussehen
       [ack_id] = unpack('<I', byte_msg[-5:-1])

       json_obj = {k: v for k, v in locals().items() if k in [
          "payload_type",
	        "msg_id",
	        "max_hop",
	        "mesh_info",
	        "message",
	        "ack_id",
	        "calced_fcs" ]}

       return json_obj

    elif bytes(byte_msg[:2]) in {b'@:', b'@!'}:

      split_idx = remaining_msg.find(b'>')
      if split_idx == -1:
        return "Kein gültiges Routing-Format"

      path = remaining_msg[:split_idx+1].decode("utf-8", errors="ignore")
      remaining_msg = remaining_msg[split_idx + 1:]

      # Extrahiere Dest-Type (`dt`)
      if payload_type == 58:
        split_idx = remaining_msg.find(b':')
      elif payload_type == 33:
        split_idx = remaining_msg.find(b'*')+1
      else:
        print(f"Payload type not matched! {payload_type}")

      if split_idx == -1:
         return "Destination not found"

      dest = remaining_msg[:split_idx].decode("utf-8", errors="ignore")

      message = remaining_msg[split_idx:remaining_msg.find(b'\00')].decode("utf-8", errors="ignore").strip()

      #Etwas bit banging, weil die Binaerdaten am Ende immer gleich aussehen
      [zero, hardware_id, lora_mod, fcs, fw, lasthw, fw_subver, ending, time_ms ] = unpack('<BBBHBBBBI', byte_msg[-14:-1])

      #Frame checksum checken
      fcs_ok = (calced_fcs == fcs)

      if message.startswith(":{CET}"):
        dest_type = "Datum & Zeit Broadcast an alle"
      
      elif path.startswith("response"):
        dest_type = "user input response"

      elif message.startswith("!"):
        dest_type = "Positionsmeldung"

      elif dest == "*":
        dest_type = "Broadcast an alle"

      elif dest.isdigit():
        dest_type = f"Gruppennachricht an {dest}"

      else:
        dest_type = f"Direktnachricht an {dest}"

      json_obj = {k: v for k, v in locals().items() if k in [
          "payload_type", 
          "msg_id",
          "max_hop",
          "mesh_info",
          "dest_type",
          "path",
          "dest",
          "message",
          "hardware_id", 
          "lora_mod", 
          "fcs", 
          "fcs_ok", 
          "fw", 
          "fw_subver", 
          "lasthw", 
          "time_ms",
          "ending" 
          ]}

      return json_obj

    else:
       return "Kein gueltiges Mesh-Format"


block_list = [
  "response",
  "OE0XXX-99",
]

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

def prune_messages():
    global message_store_size
    cutoff = datetime.utcnow() - timedelta(hours=PRUNE_HOURS)
    temp_store = deque()
    new_size = 0

    for item in message_store:
        #print(f"next item {item}")

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

    message_store.clear()
    message_store.extend(temp_store)
    message_store_size = new_size
    print(f"After message cleaning {len(message_store)}")

def load_dump():
    global message_store, message_store_size
    if os.path.exists(store_file_name):
        with open(store_file_name, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            message_store = deque(loaded)
            message_store_size = sum(len(json.dumps(m).encode("utf-8")) for m in message_store)
            print(f"{len(message_store)} Nachrichten ({message_store_size / 1024:.2f} KB) geladen")



def hex_msg_id(msg_id):
    return f"{msg_id:08X}"

def ascii_char(val):
    return chr(val)

def strip_prefix(msg, prefix=":"):
    return msg[1:] if msg.startswith(prefix) else msg

def parse_aprs_position(message):
    # APRS-Position: !4824.46N/01144.31EG...
    match = re.match(r"!(\d{2})(\d{2}\.\d{2})([NS])[/\\](\d{3})(\d{2}\.\d{2})([EW])([A-Za-z])", message)
    if not match:
        return None

    lat_deg, lat_min, lat_dir, lon_deg, lon_min, lon_dir, symbol = match.groups()
    lat = int(lat_deg) + float(lat_min)/60
    lon = int(lon_deg) + float(lon_min)/60

    if lat_dir == 'S':
        lat = -lat
    if lon_dir == 'W':
        lon = -lon

    alt_match = re.search(r"/A=(\d{6})", message)
    altitude = 0
    altitude_ft = 0
    if alt_match:
        altitude_ft = int(alt_match.group(1))
        altitude = round(altitude_ft * 0.3048, 0)

    return {
        "lat": round(lat, 4),
        "lat_dir": lat_dir,
        "long": round(lon, 4),
        "long_dir": lon_dir,
        "aprs_symbol": symbol,
        "aprs_symbol_group": "/",
        "alt": altitude_ft
    }

def timestamp_from_date_time(date, time):
    dt_str = f"{date} {time}"
    try:
       dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except Exception as e:
       dt = datetime.strptime("1970-01-01 00:00:00", "%Y-%m-%d %H:%M:%S")

    return int(dt.timestamp() * 1000)

def transform_common_fields(d):
    return {
        "firmware": d.get("fw"),
        "fw_sub": ascii_char(d.get("fw_subver")),
        "max_hop": d.get("max_hop"),
        "mesh_info": d.get("mesh_info"),
        "node_timestamp": d.get("time_ms"),
        "timestamp": int(time.time() * 1000),
        "lora_mod": d.get("lora_mod"),
        "last_hw": d.get("lasthw")
    }

def transform_msg(input_dict):
    return {
        "src_type": "lora",
        "type": "msg",
        "src": input_dict["path"].rstrip(">"),
        "dst": input_dict["dest"],
        "msg": strip_prefix(input_dict["message"]),
        "msg_id": hex_msg_id(input_dict["msg_id"]),
        "hw_id": input_dict["hardware_id"],
        **transform_common_fields(input_dict)
    }

def transform_ack(input_dict):
    return {
       "src_type": "lora",
       "type": "ack",
       "msg_id": hex_msg_id(input_dict["msg_id"]),
       "msg": input_dict["message"],
       "ack_id": hex_msg_id(input_dict["ack_id"]),
       "timestamp": int(time.time() * 1000)
    } 

def transform_pos(input_dict):
    aprs = parse_aprs_position(input_dict["message"]) or {}
    return {
        "src_type": "lora",
        "type": "pos",
        "src": input_dict["path"].rstrip(">"),
        #"msg": "",
        "msg_id": hex_msg_id(input_dict["msg_id"]),
        "hw_id": input_dict["hardware_id"],
        **aprs,
        **transform_common_fields(input_dict)
    }

def transform_mh(input_dict):
    return {
        "src_type": "lora",
        "type": "pos",
        "src": input_dict["CALL"],
        #"msg": "",
        #"lat": 0,
        #"lat_dir": "",
        #"long": 0,
        #"long_dir": "",
        #"alt": 0,
        #"aprs_symbol": "",
        "hw_id": input_dict["HW"],
        "rssi": input_dict.get("RSSI"),
        "snr": input_dict.get("SNR"),
        "pl": input_dict.get("PL"),
        "mesh": input_dict.get("MESH"),
        "node_timestamp": timestamp_from_date_time(input_dict["DATE"], input_dict["TIME"]),
        "timestamp": int(time.time() * 1000)
    }

def transform_ble(input_dict):
    return{
        "src_type": "BLE",
         **input_dict,
        "timestamp": int(time.time() * 1000)
     }


def dispatcher(input_dict):
    if "TYP" in input_dict:
        if input_dict["TYP"] == "MH":
            return transform_mh(input_dict)
        elif input_dict["TYP"] == "I":
            if has_console:
              print("Type I")
            return transform_ble(input_dict)
        elif input_dict["TYP"] == "SN":
            if has_console:
              print("Type SN")
            return transform_ble(input_dict)
        elif input_dict["TYP"] == "G":
            if has_console:
              print("Type G")
            return transform_ble(input_dict)
        elif input_dict["TYP"] == "SA":
            if has_console:
              print("Type SA")
            return transform_ble(input_dict)
        elif input_dict["TYP"] == "G":
            if has_console:
              print("Type G")
            return transform_ble(input_dict)
        elif input_dict["TYP"] == "W":
            if has_console:
              print("Type W")
            return transform_ble(input_dict)

        elif input_dict["TYP"] == "IO":
            if has_console:
              print("Type IO")
            return transform_ble(input_dict)

        elif input_dict["TYP"] == "TM":
            if has_console:
              print("Type TM")
            return transform_ble(input_dict)

        elif input_dict["TYP"] == "AN":
            if has_console:
              print("Type AN")
            return transform_ble(input_dict)

        elif input_dict["TYP"] == "SE":
            if has_console:
              print("Type SE")
            return transform_ble(input_dict)
        elif input_dict["TYP"] == "SW":
            if has_console:
              print("Type SW")
            return transform_ble(input_dict)
        else:
            if has_console:
              print("Type nicht gefunden!",input_dict)


    elif input_dict.get("payload_type") == 58:
        return transform_msg(input_dict)

    elif input_dict.get("payload_type") == 33:
        return transform_pos(input_dict)

    elif input_dict.get("payload_type") == 65:
        return transform_ack(input_dict)

    else:
        print(f"Unbekannter payload_type oder TYP: {input_dict}")

async def main():
    load_dump()
    prune_messages()

    try:
      ws_server = await websockets.serve(websocket_handler, WS_HOST, WS_PORT)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"❌ Address {WS_HOST}:{WS_PORT} already in use.")
            print("🧠 Tip: Is another instance of the server already running?")
            print("👀 Try `lsof -i :{}` or `netstat -tulpen | grep {}` to investigate.".format(WS_PORT, WS_PORT))
            print("💣 Exiting gracefully from a non recoverale error.\n")
            sys.exit(1)
        else:
            raise  # re-raise any other unexpected OSError

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
       print("🛡️ Signal empfangen, beende Dienst ...")
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
    print(f"UDP-Listen {UDP_PORT_list}, Target MeshCom {UDP_TARGET}")

    await stop_event.wait()

    print("Stopping server, svaing to disc …")

    await ble_disconnect()

    udp_task.cancel()

    ws_server.close()

    print("warten auf close.")
    await ws_server.wait_closed()

    with open(store_file_name, "w", encoding="utf-8") as f:
        json.dump(list(message_store), f, ensure_ascii=False, indent=2)
    print("Daten gespeichert.")

if __name__ == "__main__":
    client = None  # placeholder

    has_console = sys.stdout.isatty()
    config = load_config()

    UDP_PORT_list = config["UDP_PORT_list"]

    UDP_PORT_send = config["UDP_PORT_send"]
    UDP_TARGET = (config["UDP_TARGET"], UDP_PORT_send)

    WS_HOST = config["WS_HOST"]
    WS_PORT = config["WS_PORT"]

    PRUNE_HOURS = config["PRUNE_HOURS"]
    print(f"Messages older than {hours_to_dd_hhmm(PRUNE_HOURS)} get deleted")

    MAX_STORE_SIZE_MB = config["MAX_STORAGE_SIZE_MB"]
    print(f"If we get flooded with messages, we drop after {MAX_STORE_SIZE_MB}MB")

    store_file_name = config["STORE_FILE_NAME"]
    print(f"Messages will be stored on exit: {store_file_name}")


    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Manuell beendet mit Ctrl+C")

