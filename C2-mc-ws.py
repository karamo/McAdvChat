import asyncio
import json
import websockets
import socket
import os
import signal
import sys
import time
import unicodedata
from struct import *
from bleak import BleakScanner, BleakClient
from datetime import datetime, timedelta
from collections import deque

#import subprocess
#import threading

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next.errors import DBusError
from dbus_next.service import ServiceInterface, method
#from bluezero import constants

VERSION="v0.7.0"
CONFIG_FILE = "/etc/mcadvchat/config.json"


BLUEZ_SERVICE_NAME = "org.bluez"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
DEVICE_INTERFACE = "org.bluez.Device1"
GATT_CHARACTERISTIC_INTERFACE = "org.bluez.GattCharacteristic1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"

AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_PATH = "/com/example/agent"

read_char_uuid = "6e400003-b5a3-f393-e0a9-e50e24dcca9e" # UUID_Char_NOTIFY
write_char_uuid = "6e400002-b5a3-f393-e0a9-e50e24dcca9e" # UUID_Char_WRITE
hello_byte = bytes([0x04, 0x10, 0x20, 0x30])

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
    try: 
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
            print(f"[ERROR] Invalid character: '{c}' (U+{cp:04X}, {name})")
            print(f"in: {message}")
            message["msg"] = "-- invalid message suppressed --" #we remove bullshit

        message["timestamp"] = int(time.time() * 1000)
        dt = datetime.fromtimestamp(message['timestamp']/1000)
        readabel = dt.strftime("%d %b %Y %H:%M:%S")

        message["from"] = addr[0]

        if isinstance(message, dict) and isinstance(message.get("msg"), str):
            if message["msg"].startswith("{CET}"):
                if has_console:
                   print(f"{readabel} {message['src_type']} von {addr[0]} Zeit: {message['msg']} ID:{message['msg_id']} src:{message['src']}")
                   print(f"{readabel} {message['src_type']} von {addr[0]}: {message}")
            else:
                store_message(message, json.dumps(message)) #wir wollen mit Timestamp speichern
                if has_console:
                   print(f"{readabel} {message['src_type']} von {addr[0]}: {message}")

        if clients:
            send_tasks = [asyncio.ensure_future(client.send(json.dumps(message))) for client in clients]
            await asyncio.gather(*send_tasks, return_exceptions=True)

    except asyncio.CancelledError: 
        print("udp_listener was cancelled. Closing socket.")
    finally:
        udp_sock.close()


async def websocket_handler(websocket):
    peer = websocket.remote_address[0] if websocket.remote_address else "unbekannt"
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

                elif data.get("type") == "BLE":
                   await client.send_message(data.get("msg"), data.get("dst"))

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

    elif msg == "scan BLE":
        await scan_ble_devices()

    elif msg == "BLE info":
        await ble_info()

    elif msg == "pair BLE":
        await ble_pair()

    elif msg == "unpair BLE":
        await ble_unpair()

    elif msg == "disconnect BLE":
        await ble_disconnect()

    elif msg == "connect BLE":
        await ble_connect()

    elif (msg.startswith("--set") | msg.startswith("--sym")):
        await client.set_commands(msg)

    elif msg.startswith("--"):
        await client.a0_commands(msg)

    else:
        print(f"command not available", msg)

def mac_to_dbus_path(mac):
    return f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

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

    async def connect(self):
        if self.bus is None:
           self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        introspection = await self.bus.introspect(BLUEZ_SERVICE_NAME, self.path)
        self.device_obj = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, self.path, introspection)
        self.dev_iface = self.device_obj.get_interface(DEVICE_INTERFACE)
        self.props_iface = self.device_obj.get_interface(PROPERTIES_INTERFACE)

        connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        if not connected:
           try:
             await self.dev_iface.call_connect()
             print(f"✅ Neu verbunden mit {self.mac}")
           except DBusError as e:
             print(f"⚠️  Connect timeout: {e}")
             self.bus = None
             return
        else:
           print(f"🔁 Verbindung zu {self.mac} besteht bereits")

        await self._find_characteristics()

        if not self.read_char_iface or not self.write_char_iface:
            raise Exception("❌ Charakteristika nicht gefunden")
        
        self.read_props_iface = self.read_char_obj.get_interface(PROPERTIES_INTERFACE)

        is_notifying = (await self.read_props_iface.call_get(GATT_CHARACTERISTIC_INTERFACE, "Notifying")).value
        print("Notifications sind .. ", is_notifying)


    async def _find_characteristics(self):
        self.read_char_obj, self.read_char_iface = await find_gatt_characteristic(
            self.bus, self.path, self.read_uuid)
        self.write_char_obj, self.write_char_iface = await find_gatt_characteristic(
            self.bus, self.path, self.write_uuid)

    async def start_notify(self, on_change=None):
        print("▶️  Start notify ..")
        if not self.bus:
           print("❌ Connection not established, start notify aborted")
           return

        if not self.read_char_iface:
            raise Exception("read_char_iface nicht initialisiert")

        try:
            if on_change:
                self._on_value_change_cb = on_change

            self.read_props_iface.on_properties_changed(self._on_props_changed)
            await self.read_char_iface.call_start_notify()

            is_notifying = (await self.read_props_iface.call_get(GATT_CHARACTERISTIC_INTERFACE, "Notifying")).value

            print(f"📡 Notify gestartet, Status: {is_notifying}")
        except DBusError as e:
            print(f"⚠️ StartNotify fehlgeschlagen: {e}")


    def _on_props_changed(self, iface, changed, invalidated):
      if iface != GATT_CHARACTERISTIC_INTERFACE:
        return

      if "Value" in changed:
        new_value = changed["Value"].value
        notification_handler(new_value)

        if self._on_value_change_cb:
            self._on_value_change_cb(new_value)


    async def stop_notify(self):
        if not self.bus:
           print("🛑 connection not established, can't stop notify ..")
           return

        if not self.read_char_iface:
           print("🛑 no read interface, can't stop notify ..")
            return
        try:
            await self.read_char_iface.call_stop_notify()
            print("🛑 Notify gestoppt")
        except DBusError as e:
            if "No notify session started" in str(e):
                print("ℹ️ Keine Notify-Session – ignoriert")
            else:
                raise

    async def send_hello(self):
        if not self.bus:
           print("🛑 connection not established, can't send hello ..")
           return

        connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        if not connected:
           print("🛑 connection lost, can't send ..")
           await self.disconnect() #aufräumen, vielleicht hilft es etwas
           await self.close() #aufräumen, vielleicht hilft es etwas
           return

        if self.write_char_iface:
            await self.write_char_iface.call_write_value(self.hello_bytes, {})
            print(f"📨 Hello sent ..")

        else:
            print("⚠️ Keine Write-Charakteristik verfügbar")


    async def send_message(self, msg, grp):
        if not self.bus:
           print("🛑 connection not established, can't send ..")
           return

        connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        if not connected:
           print("🛑 connection lost, can't send ..")
           await self.disconnect() #aufräumen, vielleicht hilft es etwas
           await self.close() #aufräumen, vielleicht hilft es etwas
           return

        message = "{" + grp + "}" + msg
        byte_array = bytearray(message.encode('utf-8'))

        laenge = len(byte_array) + 2

        byte_array = laenge.to_bytes(1, 'big') +  bytes ([0xA0]) + byte_array

        if self.write_char_iface:
            await self.write_char_iface.call_write_value(byte_array, {})
            print(f"📨 Message sent .. {byte_array}")
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
           return

        connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        if not connected:
            print("🛑 connection lost, can't send ..")
            await self.disconnect() #aufräumen, vielleicht hilft es etwas
            print("debug: disconnect ..")
            await self.close() #aufräumen, vielleicht hilft es etwas
            print("debug: close ..")
            return

        #print(f"✅ ready to send")

        byte_array = bytearray(cmd.encode('utf-8'))

        laenge = len(byte_array) + 2

        byte_array = laenge.to_bytes(1, 'big') +  bytes ([0xA0]) + byte_array

        if self.write_char_iface:
            await self.write_char_iface.call_write_value(byte_array, {})
            print(f"📨 Message sent .. {byte_array}")

        else:
            print("⚠️ Keine Write-Charakteristik verfügbar")


       #--mheard -> gibts nicht
       #--path -> gibts nichs
    async def set_commands(self, cmd):
       laenge = 0
       print("special commands, not yet implemented")
       
       if not self.bus:
          print("🛑 connection not established, can't send ..")
          return

       connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
       if not connected:
            print("🛑 connection lost, can't send ..")
            await self.disconnect() #aufräumen, vielleicht hilft es etwas
            print("debug: disconnect ..")
            await self.close() #aufräumen, vielleicht hilft es etwas
            print("debug: close ..")
            return

       print(f"✅ ready to send")
       #ID = 0x20 Timestamp from phone [4B]
       if cmd == "--settime":
         cmd_byte = bytes([0x20])

         time_str=get_current_timestamp()
         time_str=time_str.replace("T", " ").split(".")[0]
         print(f"Aktuelle Zeit {time_str}")

         byte_array = bytearray(time_str.encode('utf-8'))

         laenge = len(byte_array) + 3
         byte_array = laenge.to_bytes(1, 'big') +  cmd_byte + byte_array + bytes([0x4b])

         if self.write_char_iface:
            await self.write_char_iface.call_write_value(byte_array, {})
            print(f"📨 Message sent .. {byte_array}")

         else:
            print("⚠️ Keine Write-Charakteristik verfügbar")


       elif cmd == "--setCALL":
         cmd_byte = bytes([0x50])
         #0x50 - Callsign (--setCALL) [1B len - Callsign]

       elif (cmd == "--setSSID" | cmd == "--setPWD"):
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

       elif (cmd == "--symID" | cmd =="--symCD"):
         param="G"
         cmd_byte = bytes([0x90])
         #0x95 - APRS Symbols (--symID --symCD)
         laenge=len(param)

       elif cmd == "--setFlash":
       #0xF0 - Save Settings to Flash
         cmd_byte = bytes([0xF0])

       #     save_flag is 0x0A for save and 0x0B for don't save
       #(Aus Z.365ff https://github.com/icssw-org/MeshCom-Firmware/blob/oe1kfr_434q/src/phone_commands.cpp)
       
       print(f"alles zusammen und raus damit {cmd_byte} {laenge}")
   

    async def disconnect(self):
        print("⬇️ disconnect ..")

        if not self.dev_iface:
            return
        try:
            await self.stop_notify()
            await self.dev_iface.call_disconnect()
            print(f"🧹 Disconnected von {self.mac}")
        except DBusError as e:
            print(f"⚠️ Disconnect fehlgeschlagen: {e}")

    async def close(self):
        if self.bus:
            self.bus.disconnect()
        self.bus = None

    async def ble_info(self):
      if not self.props_iface:
          print("⚠️  not connected, can't ask for info")
          return

      try:
        props = await self.props_iface.call_get_all(DEVICE_INTERFACE)

        print("🔍 BLE Device Info:")
        for key, val in props.items():
            print(f"  {key}: {val.value}")
      except Exception as e:
        print(f"❌ Failed to fetch info for {self.props_iface}: {e}")

    async def scan_ble_devices(self, timeout=10.0):
      print("🔍 Starting native BLE scan via BlueZ... timout =",timeout)
      if self.bus is None:
          self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
      else:
          print("uups, already connected? Why scan again?")

      path = "/org/bluez/hci0"

      introspection = await self.bus.introspect(BLUEZ_SERVICE_NAME, path)
      device_obj = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, introspection)
      self.adapter = device_obj.get_interface(ADAPTER_INTERFACE)
     
      # Track discovered devices
      self.found_devices = {}

      # Event zur Synchronisation
      found_mc_event = asyncio.Event()

      # Listen to InterfacesAdded signal
      async def _interfaces_added(path, interfaces):
        if DEVICE_INTERFACE in interfaces:
            props = interfaces[DEVICE_INTERFACE]
            name = props.get("Name", Variant("s", "")).value
            addr = props.get("Address", Variant("s", "")).value
            rssi = props.get("RSSI", Variant("n", 0)).value
            self.found_devices[path] = (name, addr, rssi)
            print(f"🔹 {name} | Address: {addr} | RSSI: {rssi}")

            if name.startswith("MC-"):
                print("✅ Matching device found. Stopping discovery early...")
                found_mc_event.set()

      # Subscribe to the signal
      self.obj_mgr = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, "/", await self.bus.introspect(BLUEZ_SERVICE_NAME, "/"))
      self.obj_mgr_iface = self.obj_mgr.get_interface(OBJECT_MANAGER_INTERFACE)
      self.obj_mgr_iface.on_interfaces_added(_interfaces_added)

      # Start discovery
      await self.adapter.call_start_discovery()

      try:
        # Warte bis entweder Gerät gefunden oder Timeout abgelaufen
        await asyncio.wait_for(found_mc_event.wait(), timeout)
      except asyncio.TimeoutError:
        print(f"⏱ Timeout expired after {timeout:.1f}s, no matching device found.")

      await self.adapter.call_stop_discovery()

      print(f"\n✅ Scan complete. Found {len(self.found_devices)} device(s):")
      for path, (name, addr, rssi) in self.found_devices.items():
          print(f"🔹 {name} | Address: {addr} | RSSI: {rssi}")

      await self.disconnect()
      #self.bus.disconnect()

async def ble_info():
    print("BLE info dummy");
    await client.ble_info()

#    mac = "D4:D4:DA:9E:B5:62"
#    path = mac_to_dbus_path(mac)

#    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

#    try:
#        device_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, path,
#                                          await bus.introspect(BLUEZ_SERVICE_NAME, path))
#        dev_iface = device_obj.get_interface(DEVICE_INTERFACE)
        #props_iface = device_obj.get_interface("org.freedesktop.DBus.Properties")
#        props_iface = device_obj.get_interface(PROPERTIES_INTERFACE)

#        props = await props_iface.call_get_all(DEVICE_INTERFACE)

#        print("🔍 BLE Device Info:")
#        for key, val in props.items():
            # Handle Variant wrapper
#            print(f"  {key}: {val.value}")

#    except Exception as e:
#        print(f"❌ Failed to fetch info for {mac}: {e}")

class NoInputNoOutputAgent(ServiceInterface):
    def __init__(self):
        super().__init__('org.bluez.Agent1')

    @method()
    def Release(self):
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

async def ble_unpair():
    mac="D4:D4:DA:9E:B5:62"
    print(f"🧹 Unpairing {mac} using blueZ ...")

    device_path = mac_to_dbus_path(mac)
    adapter_path = "/org/bluez/hci0"

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    # Unpairing logic
    adapter_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, adapter_path,
                                   await bus.introspect(BLUEZ_SERVICE_NAME, adapter_path))
    adapter_iface = adapter_obj.get_interface("org.bluez.Adapter1")

    await adapter_iface.call_remove_device(device_path)
    print(f"🧹 Unpaired device {mac}")


async def ble_pair():
    mac="D4:D4:DA:9E:B5:62"

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
    dev_iface = dev_obj.get_interface(DEVICE_INTERFACE)

    try:
        await dev_iface.call_pair()
        print(f"✅ Successfully paired with {mac}")

        await dev_iface.set_trusted(True)
        print(f"🔐 Device {mac} marked as trusted.")

        is_paired = await dev_iface.get_paired()
        print(f"📎 Paired state of {mac}: {is_paired}")

        is_trusted = await dev_iface.get_trusted()
        print(f"Trust state: {is_trusted}")

        is_bonded = await dev_iface.get_bonded()
        print(f"Bond state: {is_bonded}")

        await asyncio.sleep(2)  # allow time for registration to settle

        #try:
        #   await dev_iface.call_disconnect()
        #   print(f"🔌 Disconnected from {mac} after pairing.")
        #except Exception as e:
        #   print(f"⚠️ Could not disconnect from {mac}: {e}")

    except Exception as e:
        print(f"❌ Failed to pair with {mac}: {e}")




#def handle_notification(interface_name, changed, invalidated):
#    print(f"🔥 PropertiesChanged: {interface_name}, changed: {changed}")
#    if "Value" in changed:
#        data = bytes(changed["Value"].value)
#        print(f"📨 Notification received: {data}")
#    else:
#        print(f"🔥 PropertiesChanged: {interface_name}, changed: {changed}")

#def on_props_changed(interface_name, changed, invalidated):
#    if "Value" in changed:
#        handle_notification(interface_name, changed, invalidated)

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

async def ble_disconnect():
        print("BLE disconnect dummy");
        await client.disconnect()
        await client.close()

        #mac = "D4:D4:DA:9E:B5:62"  # Example MAC
        #path = mac_to_dbus_path(mac)
        #bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    #try:
        #device_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, await bus.introspect(BLUEZ_SERVICE_NAME, path))
        #dev_iface = device_obj.get_interface(DEVICE_INTERFACE)

        #read_char_obj, read_char_iface = await find_gatt_characteristic(bus, path, read_char_uuid)
        #props_iface = read_char_obj.get_interface(PROPERTIES_INTERFACE)

        #props = await props_iface.call_get_all(GATT_CHARACTERISTIC_INTERFACE)
        #print("Props", props);

        #is_notifying = (await props_iface.call_get(GATT_CHARACTERISTIC_INTERFACE, "Notifying")).value
        #print("Notifications sind ..", is_notifying);
       
        #if is_notifying:
        #   await read_char_iface.call_stop_notify()
        #   print("🛑 Notifications gestoppt")
        #else:
        #   print("ℹ️ Keine aktive Notify-Session – StopNotify() übersprungen") 

        # Call the disconnect method
        #await dev_iface.call_disconnect()
        #print(f"✅ Successfully disconnected from {mac}")
    #except Exception as e:
    #    print(f"❌ Failed to disconnect from {mac}: {e}")


async def ble_connect():
    #print("BLE connect dummy");
    #client = BLEClient(
    #    mac ="D4:D4:DA:9E:B5:62",
    #    read_uuid = "6e400003-b5a3-f393-e0a9-e50e24dcca9e", # UUID_Char_NOTIFY
    #    write_uuid = "6e400002-b5a3-f393-e0a9-e50e24dcca9e", # UUID_Char_WRITE
    #    hello_bytes = b'\x04\x10\x20\x30'
    #)

    await client.connect()
    await client.start_notify()
    await client.send_hello()
    #return client

    #mac = "D4:D4:DA:9E:B5:62"  # Your target BLE MAC
    #path = mac_to_dbus_path(mac)
    #bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    #try:
    #    device_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, await bus.introspect(BLUEZ_SERVICE_NAME, path))
    #    dev_iface = device_obj.get_interface(DEVICE_INTERFACE)

    #    # Call connect
    #    await dev_iface.call_connect()
    #    print(f"✅ Successfully connected to {mac}")

    #    read_char_obj, read_char_iface = await find_gatt_characteristic(bus, path, read_char_uuid)
    #    write_char_obj, write_char_iface = await find_gatt_characteristic(bus, path, write_char_uuid)

    #    if not read_char_iface or not write_char_iface:
    #        print("❌ Could not find required characteristics")
    #        return

    #    props_iface = read_char_obj.get_interface(PROPERTIES_INTERFACE)
    #    is_notifying = (await props_iface.call_get(GATT_CHARACTERISTIC_INTERFACE, "Notifying")).value
    #    print("Notifications sind .. ", is_notifying)

    #    if (is_notifying):
    #      print("📡 Notifications waren schon gestartet")
    #    else:
    #      props_iface.on_properties_changed(handle_notification)
    #      await read_char_iface.call_start_notify()
    #      print("📡 Notifications gestartet")

    #    # HELLO - aufwachen, es geht los
    #    await write_char_iface.call_write_value(bytes(hello_byte), {})

    #    #await read_char_iface.call_stop_notify()
    #except Exception as e:
    #   print(f"❌ Failed to connect to {mac}: {e}")

async def scan_ble_devices():
     await client.scan_ble_devices()

#    print("Please give me a second to complete bluetooth scan for MC-* ..")
#    devices = await BleakScanner.discover()
#    for device in devices:
#      #if device.name.startswith("MC-"):
#        print(f"Device {device.name}, Address: {device.address}, RSSI: {device._rssi}")
#
#    print(f"finished scanning ..")


def notification_handler(clean_msg):

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
               "CONFFIN": "Habe fertig"
         }

         try:
           typ = var.get('TYP')

           print("type_map",typ_mapping.get(var.get('TYP'), var))

           if typ == 'MH': # MH update
             print("MH",var)

           elif typ == "SA": # APRS.fi Info
             print("APRS")

           elif typ == "G": # GPS Info
             print("GPS")

           elif typ == "W": # Wetter Info
             print("Wetter")

           elif typ == "SN": # System Settings wie Buttung 
             print("System Settings")

           elif typ == "SE": # System Settings wie Buttung 
             print("Druck und Co Sensoren")

           elif typ == "SW": # WIFI + IP Settings
             print("Wifi Settings")

           elif typ == "I": # Info Seite
             print("Info Seite", var)

           elif typ == "CONFFIN": # Habe Fertig! Mehr gibt es nicht
             print("Habe fertig")

         except KeyError:
             print(error,var) 

    # Binärnachrichten beginnen mit '@'
    elif clean_msg.startswith(b'@'):
      print("bin decode",decode_binary_message(clean_msg))

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

#BLE Handling
def decode_json_message(byte_msg):
    try:
        json_str = byte_msg.rstrip(b'\x00').decode("utf-8")[1:]
        return json.loads(json_str)

    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"Fehler beim Dekodieren der JSON-Nachricht: {e}")
        return None

#BLE Handling
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

    if byte_msg[:2] == b'@A':  # Prüfen, ob es sich umACK Frames handel

       #remaining_msg = byte_msg[8:].rstrip(b'\x00')  # Alles nach Hop
       message = remaining_msg.hex().upper()

       #Etwas bit banging, weil die Binaerdaten am Ende immer gleich aussehen
       #[zero, hardware_id, lora_mod, fcs, fw, lasthw, fw_subver, ending, time_ms ] = unpack('<BBBHBBBBI', byte_msg[-14:-1])
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
      #remaining_msg = byte_msg[7:]  # Alles nach Hop
      # Extrahiere den Path

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
    print(f"UDP-Proxy {UDP_PORT_list}, MeshCom {UDP_TARGET}")

    await stop_event.wait()

    print("Beende Server, speichere Daten …")

#### BLE Handling ################
    await client.disconnect()
    print("nach BLE disconnect …")
    await client.close()
    print("nach BLE close …")
##################################


    udp_task.cancel()
    print("nach udp_task.cancel …")
    ws_server.close()
    print("nach ws_server.close …")

    print("warten auf close.")
    await ws_server.wait_closed()

    with open(store_file_name, "w", encoding="utf-8") as f:
        json.dump(list(message_store), f, ensure_ascii=False, indent=2)
    print("Daten gespeichert.")


if __name__ == "__main__":
    client = BLEClient(
        mac ="D4:D4:DA:9E:B5:62",
        read_uuid = "6e400003-b5a3-f393-e0a9-e50e24dcca9e", # UUID_Char_NOTIFY
        write_uuid = "6e400002-b5a3-f393-e0a9-e50e24dcca9e", # UUID_Char_WRITE
        hello_bytes = b'\x04\x10\x20\x30'
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Manuell beendet mit Ctrl+C")

