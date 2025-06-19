#!/usr/bin/env python3
import asyncio
import json
import time
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from timezonefinder import TimezoneFinder
from struct import *

from dbus_next import Variant, MessageType
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next.errors import DBusError, InterfaceNotFoundError
from dbus_next.service import ServiceInterface, method

VERSION="v0.48.0"

has_console = sys.stdout.isatty()

# DBus constants
BLUEZ_SERVICE_NAME = "org.bluez"
AGENT_INTERFACE = "org.bluez.Agent1"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
DEVICE_INTERFACE = "org.bluez.Device1"
GATT_CHARACTERISTIC_INTERFACE = "org.bluez.GattCharacteristic1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
AGENT_PATH = "/com/example/agent"

# Global client instance (managed by this module)
client = None

# Console detection
has_console = sys.stdout.isatty()



def get_current_timestamp() -> str:
    """Get current UTC timestamp in ISO format"""
    return datetime.utcnow().isoformat()


def calc_fcs(msg):
    """Calculate frame checksum"""
    fcs = 0
    for x in range(0, len(msg)):
        fcs = fcs + msg[x]
    
    # SWAP MSB/LSB
    fcs = ((fcs & 0xFF00) >> 8) | ((fcs & 0xFF) << 8)
    
    return fcs


def hex_msg_id(msg_id):
    """Convert message ID to hex string"""
    return f"{msg_id:08X}"


def ascii_char(val):
    """Convert value to ASCII character"""
    return chr(val)


def strip_prefix(msg, prefix=":"):
    """Strip prefix from message if present"""
    return msg[1:] if msg.startswith(prefix) else msg


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
        # ACK Message Format: [0x41] [MSG_ID-4] [FLAGS] [ACK_MSG_ID-4] [ACK_TYPE] [0x00]
        
        # FLAGS byte (max_hop_raw) dekodieren
        server_flag = bool(max_hop_raw & 0x80)  # Bit 7: Server Flag
        hop_count = max_hop_raw & 0x7F  # Bits 0-6: Hop Count
        
        # ACK spezifische Felder extrahieren
        if len(byte_msg) >= 12:
            # ACK_MSG_ID (die Original Message ID die bestätigt wird)
            [ack_id] = unpack('<I', byte_msg[6:10])
            
            # ACK_TYPE
            ack_type = byte_msg[10] if len(byte_msg) > 10 else 0
            ack_type_text = "Node ACK" if ack_type == 0x00 else "Gateway ACK" if ack_type == 0x01 else f"Unknown ({ack_type})"
            
            # Gateway ID und ACK ID aus der msg_id extrahieren (wenn es ein Gateway ACK ist)
            if ack_type == 0x01:
                gateway_id = (msg_id >> 10) & 0x3FFFFF  # Bits 31-10: Gateway ID (22 Bits)
                ack_id_part = msg_id & 0x3FF  # Bits 9-0: ACK ID (10 Bits)
            else:
                gateway_id = None
                ack_id_part = None
        else:
            # Fallback für alte Implementierung
            [ack_id] = unpack('<I', byte_msg[-5:-1])
            ack_type = None
            ack_type_text = None
            server_flag = None
            hop_count = max_hop
            gateway_id = None
            ack_id_part = None

        # Message als Hex darstellen
        [message] = unpack(f'<{len(remaining_msg)}s', remaining_msg)
        message = message.hex().upper()

        json_obj = {
            "payload_type": payload_type,
            "msg_id": msg_id,
            "max_hop": max_hop,
            "mesh_info": mesh_info,
            "message": message,
            "ack_id": ack_id,
            "ack_type": ack_type,
            "ack_type_text": ack_type_text,
            "server_flag": server_flag,
            "hop_count": hop_count,
            "gateway_id": gateway_id,
            "ack_id_part": ack_id_part
        }

        # Entferne None-Werte für sauberere JSON
        json_obj = {k: v for k, v in json_obj.items() if v is not None}

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
      [zero, hardware_id, lora_mod, fcs, fw, lasthw, fw_sub, ending, time_ms ] = unpack('<BBBHBBBBI', byte_msg[-14:-1])


      # lasthw aufteilen
      last_hw_id = lasthw & 0x7F        # Bits 0-6: Hardware-Typ (0-127)
      last_sending = bool(lasthw & 0x80) # Bit 7: Last Sending Flag (True/False)

      #Frame checksum checken
      fcs_ok = (calced_fcs == fcs)

      #if message.startswith(":{CET}"):
      #  dest_type = "Datum & Zeit Broadcast an alle"
      
      #elif path.startswith("response"):
      #  dest_type = "user input response"

      #elif message.startswith("!"):
      #  dest_type = "Positionsmeldung"

      #elif dest == "*":
      #  dest_type = "Broadcast an alle"

      #elif dest.isdigit():
      #  dest_type = f"Gruppennachricht an {dest}"

      #else:
      #  dest_type = f"Direktnachricht an {dest}"

#      json_obj = {k: v for k, v in locals().items() if k in [
#          "payload_type", 
#          "msg_id",
#          "max_hop",
#          "mesh_info",
#          "dest_type",
#          "path",
#          "dest",
#          "message",
#          "hardware_id", 
#          "lora_mod", 
#          "fcs", 
#          "fcs_ok", 
#          "fw", 
#          "fw_subver", 
#          "lasthw", 
#          "time_ms",
#          "ending" 
#          ]}

      json_obj = {k: v for k, v in locals().items() if k in [
          "payload_type", 
          "msg_id",
          "max_hop",
          "mesh_info",
          "path",
          "dest",
          "message",
          "hardware_id", 
          "lora_mod", 
          "fw", 
          "fw_sub", 
          "last_hw_id",
          "last_sending"
          ]}

      return json_obj

    else:
       return "Kein gueltiges Mesh-Format"


def get_timezone_info(lat, lon):
    """Get timezone information for coordinates"""
    tf = TimezoneFinder()
    tz_name = tf.timezone_at(lat=lat, lng=lon)
          
    if not tz_name:
        print("❌ Could not determine timezone")
        return None

    # Use system time (UTC) and apply tz_name
    now_utc = datetime.utcnow()
    dt_local = datetime.fromtimestamp(now_utc.timestamp(), ZoneInfo(tz_name))
    
    return {
        "timezone": tz_name,
        "offset_hours": dt_local.utcoffset().total_seconds() / 3600
    }


def timestamp_from_date_time(date, time_str):
    """Convert date and time strings to timestamp"""
    dt_str = f"{date} {time_str}"
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except Exception as e:
        dt = datetime.strptime("1970-01-01 00:00:00", "%Y-%m-%d %H:%M:%S")

    return int(dt.timestamp() * 1000)


def safe_timestamp_from_dict(input_dict):
    """Safely extract timestamp from dict with various formats"""
    date_str = input_dict.get("DATE")
    time_str = input_dict.get("TIME")

    if not date_str:
        return None

    try:
        # Case 1: Full datetime string in DATE field
        if " " in date_str and not time_str:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        # Case 2: Separate DATE and TIME fields
        elif time_str:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
        # Case 3: Date only, assume midnight
        else:
            dt = datetime.strptime(date_str, "%Y-%m-%d")

        timestamp_ms = int(dt.timestamp() * 1000)
        return timestamp_ms

    except Exception as e:
        print(f"❌ Failed to parse date/time: {e}")
        return None


def node_time_checker(node_timestamp, typ=""):
    """Check time difference between node and current time"""
    current_time = int(time.time() * 1000)  # current time in ms

    time_delta_ms = current_time - node_timestamp
    time_delta_s = time_delta_ms / 1000

    if abs(time_delta_s) > 60:
        print("⏱️ Time difference > 60 seconds")
        # Human-readable time
        current_dt = datetime.fromtimestamp(current_time / 1000)
        node_dt = datetime.fromtimestamp(node_timestamp / 1000)

        print("curr ", current_dt.strftime("%d %b %Y %H:%M:%S"))
        print("node ", node_dt.strftime("%d %b %Y %H:%M:%S"))

    return time_delta_s


def parse_aprs_position(message):
    """Parse APRS position format"""
    import re
    # Extended APRS position format with optional symbol and symbol group
    match = re.match(
        r"!(\d{2})(\d{2}\.\d{2})([NS])([/\\])(\d{3})(\d{2}\.\d{2})([EW])([ -~]?)",
        message
    )
    if not match:
        return None

    lat_deg, lat_min, lat_dir, symbol_group, lon_deg, lon_min, lon_dir, symbol = match.groups()

    lat = int(lat_deg) + float(lat_min) / 60
    lon = int(lon_deg) + float(lon_min) / 60

    if lat_dir == 'S':
        lat = -lat
    if lon_dir == 'W':
        lon = -lon

    result = {
        "transformer2": "APRS",
        "lat": round(lat, 4),
        #"lat_dir": lat_dir,
        "long": round(lon, 4),
        #"long_dir": lon_dir,
        "aprs_symbol": symbol or "?",
        "aprs_symbol_group": symbol_group,
    }

    # Altitude in feet: /A=001526
    alt_match = re.search(r"/A=(\d{6})", message)
    if alt_match:
        altitude_ft = int(alt_match.group(1))
        result["alt"] = altitude_ft

    # Battery level: /B=085
    battery_match = re.search(r"/B=(\d{3})", message)
    if battery_match:
        result["battery_level"] = int(battery_match.group(1))

    # Groups: /R=...;...;...
    group_match = re.search(r"/R=((?:\d{1,5};?){1,6})", message)
    if group_match:
        groups = group_match.group(1).split(";")
        for i, g in enumerate(groups):
            if g.isdigit():
                result[f"group_{i}"] = int(g)

    return result


def transform_common_fields(input_dict):
    return {
        "transformer1": "common_fields",
        "src_type": "ble",
        #"firmware": str(input_dict.get("fw","")) + ascii_char(input_dict.get("fw_subver")),
        "firmware": input_dict.get("fw",""),
        #"fw_sub": input_dict.get("fw_sub"),
        "fw_sub": ascii_char(input_dict.get("fw_sub")) if input_dict.get("fw_sub") else None,
        "via": input_dict.get("path"),
        "max_hop": input_dict.get("max_hop"),
        "mesh_info": input_dict.get("mesh_info"),
        "lora_mod": input_dict.get("lora_mod"),
        "last_hw_id": input_dict.get("last_hw_id"),
        "last_sending": input_dict.get("last_sending"),
        "timestamp": int(time.time() * 1000),
    }


def transform_msg(input_dict):
    return {
        "transformer": "msg",
        "src_type": "ble",
        "type": "msg",
        **input_dict,
        "src": input_dict["path"].rstrip(">"),
        "dst": input_dict["dest"],
        "msg": strip_prefix(input_dict["message"]),
        "msg_id": hex_msg_id(input_dict["msg_id"]),
        "hw_id": input_dict["hardware_id"],
        **transform_common_fields(input_dict)
    }


def transform_ack(input_dict):
    return {
       "transformer": "ack",
       "src_type": "ble",
       "type": "ack",
       **input_dict,
       "msg_id": format(input_dict.get("msg_id"), '08X'),
       "ack_id": format(input_dict.get("ack_id"), '08X'),
       "timestamp": int(time.time() * 1000)
    } 


def transform_pos(input_dict):
    aprs = parse_aprs_position(input_dict["message"]) or {}
    return {
        "transformer": "pos",
        "type": "pos",
        "src": input_dict["path"].rstrip(">"),
        "via": input_dict.get("path"),
        "msg_id": hex_msg_id(input_dict["msg_id"]),
        "msg": input_dict["message"],
        "hw_id": input_dict.get("hardware_id"),
        **aprs,
        **transform_common_fields(input_dict)
    }


def transform_mh(input_dict):
    node_timestamp = timestamp_from_date_time(input_dict["DATE"], input_dict["TIME"])
    return {
        "transformer": "mh",
        "src_type": "ble",
        "type": "pos",
        "src": input_dict["CALL"],
        "rssi": input_dict.get("RSSI"),
        "snr": input_dict.get("SNR"),
        "hw_id": input_dict.get("HW"),
        "lora_mod": input_dict.get("MOD"),
        "mesh": input_dict.get("MESH"),
        "node_timestamp": node_timestamp,
        "timestamp": node_timestamp
    }


def transform_ble(input_dict):
    return{
        "transformer": "generic_ble",
        "src_type": "BLE",
         **input_dict,
        "timestamp": int(time.time() * 1000)
     }


def dispatcher(input_dict):
    """Dispatch messages to appropriate transformer based on type"""
    if "TYP" in input_dict:
        if input_dict["TYP"] == "MH":
            return transform_mh(input_dict)
        elif input_dict["TYP"] in ["I", "SN", "G", "SA", "W", "IO", "TM", "AN", "SE", "SW"]:
            if has_console:
                print(f"Type {input_dict['TYP']}")
            return transform_ble(input_dict)
        else:
            if has_console:
                print("Type nicht gefunden!", input_dict)

    elif input_dict.get("payload_type") == 58:
        return transform_msg(input_dict)

    elif input_dict.get("payload_type") == 33:
        return transform_pos(input_dict)

    elif input_dict.get("payload_type") == 65:
        return transform_ack(input_dict)
        #print(json.dumps(input_dict, indent=2, ensure_ascii=False))
        #transformed = transform_ack(input_dict)
        #print(json.dumps(transformed, indent=2, ensure_ascii=False))
        #return transformed

    else:
        print(f"Unbekannter payload_type oder TYP: {input_dict}")


async def notification_handler(clean_msg, message_router=None):
    """Handle BLE notifications"""
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

           if typ == 'MH': # MH update
             output = dispatcher(var)
             if message_router:
                    await message_router.publish('ble', 'ble_notification', output)

           elif typ == "SA": # APRS.fi Info
             output = dispatcher(var)
             if message_router:
                   await message_router.publish('ble', 'ble_notification', output)

           elif typ == "G": # GPS Info
             global client
             if client and client._connected:
                 await client.process_gps_message(var)
             output = dispatcher(var)
             if message_router:
                    await message_router.publish('ble', 'ble_notification', output)

           elif typ == "W": # Wetter Info
             output = dispatcher(var)
             if message_router:
                   await message_router.publish('ble', 'ble_notification', output)

           elif typ in ["SN", "SE", "SW", "I", "IO", "TM", "AN"]:  # System Settings etc.
                output = dispatcher(var)
                if message_router:
                    await message_router.publish('ble', 'ble_notification', output)

           elif typ == "CONFFIN": # Habe Fertig! Mehr gibt es nicht
             if message_router:
                    await message_router.publish('ble', 'ble_status', {
                        'src_type': 'BLE',
                        'TYP': 'blueZ',
                        'command': 'conffin',
                        'result': 'ok',
                        'msg': "✅ finished sending config",
                        'timestamp': int(time.time() * 1000)
                    })

           else:
             if has_console:
                print("type unknown",var)

         except KeyError:
             print("error", var) 

    # Binärnachrichten beginnen mit '@'
    elif clean_msg.startswith(b'@'):
      message = decode_binary_message(clean_msg)

      output = dispatcher(message)
      if message_router:
            await message_router.publish('ble', 'ble_notification', output)

    else:
        print("Unbekannter Nachrichtentyp.")


class TimeSyncTask:
    def __init__(self, coro_fn):
        self._coro_fn = coro_fn
        self._event = asyncio.Event()
        self._running = False
        self._task = None

        self.lat = None
        self.lon = None

    def trigger(self, lat, lon):
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(self._set_data, lat, lon)

    def _set_data(self, lat, lon):
        self.lat = lat
        self.lon = lon
        self._event.set()

    async def runner(self):
        self._running = True
        while self._running:
            await self._event.wait()
            self._event.clear()

            if not self._running:
               break

            if None in (self.lat, self.lon):
                print("Warning: missing input data, skipping task")
                continue

            try:
              if self._running:
                await self._coro_fn(self.lat, self.lon)
            except Exception as e:
                print(f"Error during async task: {e}")

    def start(self):
        self._task = asyncio.create_task(self.runner())

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass  # Expected when we cancel
        self._task = None


class BLEClient:
    def __init__(self, mac, read_uuid, write_uuid, hello_bytes=None, message_router=None):
        self.mac = mac
        self.read_uuid = read_uuid
        self.write_uuid = write_uuid
        self.hello_bytes = hello_bytes or b'\x00'
        self.message_router = message_router
        self.path = self._mac_to_dbus_path(mac)
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
        self._keepalive_task = None
        self._time_sync = None

    def _mac_to_dbus_path(self, mac):
        """Convert MAC address to D-Bus device path"""
        return f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"


    async def connect(self, max_retries=3):
        """Connect to BLE device with retry logic and proper error handling"""
        async with self._connect_lock:
            if self._connected:
                if has_console:
                    print(f"🔁 Verbindung zu {self.mac} besteht bereits")
                return
    
            last_error = None
            for attempt in range(max_retries):
                try:
                    await self._attempt_connection()
                    return  # Success
                except Exception as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        #wait_time = min(2 ** attempt, 8)  # Exponential backoff, capped at 8 seconds
                        wait_time = 1  # linear .. we don't want to wait forever
                        if has_console:
                            print(f"⚠️ Connection attempt {attempt + 1}/{max_retries} failed: {e}")
                            print(f"🔄 Retrying in {wait_time}s...")
                        await self._publish_status('connect BLE', 'info', 
                                                 f"Attempt {attempt + 1} failed, retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        await self._cleanup_failed_connection()
                    else:
                        # This is the final attempt
                        if has_console:
                            print(f"❌ All {max_retries} connection attempts failed")
            
            # All attempts failed
            await self._publish_status('connect BLE result', 'error', 
                                     f"❌ Connection failed after {max_retries} attempts: {last_error}")
            self._connected = False


    async def _attempt_connection(self):
        """Single connection attempt - extracted from current connect() method"""
        if self.bus is None:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    
        introspection = await self.bus.introspect(BLUEZ_SERVICE_NAME, self.path)
        self.device_obj = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, self.path, introspection)
        
        try:
            self.dev_iface = self.device_obj.get_interface(DEVICE_INTERFACE)
        except InterfaceNotFoundError as e:
            raise ConnectionError(f"Interface not found, device not paired: {e}")
    
        self.props_iface = self.device_obj.get_interface(PROPERTIES_INTERFACE)
    
        try:
            connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        except DBusError as e:
            raise ConnectionError(f"Error checking connection state: {e}")
    
        if not connected:
            try:
                # Add timeout to prevent hanging
                await asyncio.wait_for(self.dev_iface.call_connect(), timeout=10.0)
                if has_console:
                    print(f"✅ verbunden mit {self.mac}")
            except asyncio.TimeoutError:
                raise ConnectionError("Connection timeout after 10 seconds")
            except DBusError as e:
                raise ConnectionError(f"Connect failed: {e}")
        else:
            if has_console:
                print(f"🔁 Verbindung zu {self.mac} besteht bereits")

        if has_console:
            print("🔍 Waiting for service discovery...")
    
        services_resolved = await self._wait_for_services_resolved(timeout=10.0)
        if not services_resolved:
            raise ConnectionError("Services not resolved within 10 seconds")

        if has_console:
            print("✅ All services discovered and resolved")
    
        await self._find_characteristics()
    
        if not self.read_char_iface or not self.write_char_iface:
            raise ConnectionError("Characteristics not found - device not properly paired")
        
        self.read_props_iface = self.read_char_obj.get_interface(PROPERTIES_INTERFACE)
    
        # Verify services are resolved
        #try:
        #    services_resolved = (await self.props_iface.call_get(DEVICE_INTERFACE, "ServicesResolved")).value
        #    if not services_resolved:
        #        # Wait a bit for services to resolve
        #        await asyncio.sleep(2)
        #        services_resolved = (await self.props_iface.call_get(DEVICE_INTERFACE, "ServicesResolved")).value
        #        if not services_resolved:
        #            raise ConnectionError("Services not resolved after connection")
        #except DBusError as e:
        #    if has_console:
        #        print(f"⚠️ Warning: Could not check ServicesResolved: {e}")
    
        self._connected = True
        await self._publish_status('connect BLE result', 'ok', "connection established, downloading config ..")
    
        # Start background tasks
        if has_console:
            print("▶️  Starting time sync task ..")
        self._time_sync = TimeSyncTask(self._handle_timesync)
        self._time_sync.start()
       
        if has_console:
            print("▶️  Starting keep alive ..")
        if not self._keepalive_task or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._send_keepalive())

    async def _wait_for_services_resolved(self, timeout=10.0):
        """Wait for BLE services to be discovered and resolved"""
        start_time = time.time()
        
        while (time.time() - start_time) < timeout:
            try:
                services_resolved = (await self.props_iface.call_get(DEVICE_INTERFACE, "ServicesResolved")).value
                if services_resolved:
                    if has_console:
                        print(f"🔍 Services resolved after {time.time() - start_time:.1f}s")
                    return True
                    
                # Still waiting - check every 500ms
                await asyncio.sleep(0.5)
                
            except DBusError as e:
                if has_console:
                    print(f"⚠️ Error checking ServicesResolved: {e}")
                await asyncio.sleep(0.5)
        
        return False

    
    async def _cleanup_failed_connection(self):
        """Clean up after a failed connection attempt"""
        try:
            if self.dev_iface:
                try:
                    await asyncio.wait_for(self.dev_iface.call_disconnect(), timeout=3.0)
                except:
                    pass  # Ignore errors during cleanup
            
            if self.bus:
                self.bus.disconnect()
            
            # Reset all state
            self.bus = None
            self.device_obj = None
            self.dev_iface = None
            self.read_char_iface = None
            self.read_props_iface = None
            self.write_char_iface = None
            self.props_iface = None
            self._connected = False
            
            # Stop background tasks if they exist
            if self._time_sync is not None:
                await self._time_sync.stop()
                self._time_sync = None
    
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
                try:
                    await self._keepalive_task
                except asyncio.CancelledError:
                    pass
                self._keepalive_task = None
            
        except Exception as e:
            if has_console:
                print(f"⚠️ Error during cleanup: {e}")

                
    async def _publish_status(self, command, result, msg):
        """Helper method to publish BLE status messages through router"""
        if self.message_router:
            status_message = {
                'src_type': 'BLE', 
                'TYP': 'blueZ', 
                'command': command,
                'result': result,
                'msg': msg,
                "timestamp": int(time.time() * 1000)
            }
            await self.message_router.publish('ble', 'ble_status', status_message)
        else:
            # Fallback to console if no router
            print(f"BLE {command}: {result} - {msg}")

    async def _send_to_websocket(self, message):
        """Helper method to send messages to websocket through router"""
        if self.message_router:
            await self.message_router.publish('ble', 'websocket_message', message)
        else:
            print(f"BLE message (no router): {message}")

    async def _find_characteristics(self):
        self.read_char_obj, self.read_char_iface = await self._find_gatt_characteristic(
            self.bus, self.path, self.read_uuid)
        self.write_char_obj, self.write_char_iface = await self._find_gatt_characteristic(
            self.bus, self.path, self.write_uuid)

    async def _find_gatt_characteristic(self, bus, path, target_uuid):
        """Find GATT characteristic by UUID in the device tree"""
        try:
            introspect = await bus.introspect(BLUEZ_SERVICE_NAME, path)
        except Exception as e:
            return None, None

        for node in introspect.nodes:
            child_path = f"{path}/{node.name}"
            try:
                child_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, child_path, 
                                                await bus.introspect(BLUEZ_SERVICE_NAME, child_path))

                props_iface = child_obj.get_interface(PROPERTIES_INTERFACE)
                props = await props_iface.call_get_all(GATT_CHARACTERISTIC_INTERFACE)

                uuid = props.get("UUID").value.lower()
                if uuid == target_uuid.lower():
                    char_iface = child_obj.get_interface(GATT_CHARACTERISTIC_INTERFACE)
                    return child_obj, char_iface

            except Exception:
                # Recursive search in child nodes
                obj, iface = await self._find_gatt_characteristic(bus, child_path, target_uuid)
                if iface:
                    return obj, iface

        return None, None

    async def start_notify(self, on_change=None):
        if not self._connected: 
           await self._publish_status('notify','error', f"❌ connection not established")
           if has_console:
              print("❌ Connection not established, start notify aborted")
           return

        is_notifying = (await self.read_props_iface.call_get(GATT_CHARACTERISTIC_INTERFACE, "Notifying")).value
        if is_notifying:
           if has_console:
              print("wir haben schon ein notify, also nix wie weg hier")
           return

        if not self.bus:
           print("❌ Connection not established, start notify aborted")
           await self._publish_status('notify','error', f"❌ connection not established")
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
               print(f"📡 Notify: {is_notifying}")
        except DBusError as e:
            print(f"⚠️ StartNotify fehlgeschlagen: {e}")

    async def _on_props_changed(self, iface, changed, invalidated):
      connection_state = "unknown"
      try:
           if self.props_iface:
                 connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
                 connection_state = "connected" if connected else "disconnected"
      except:
             connection_state = "error_checking"


      if iface != GATT_CHARACTERISTIC_INTERFACE:
        return

      if "Value" in changed:
        new_value = changed["Value"].value
        
        await notification_handler(new_value, message_router=self.message_router)

        if self._on_value_change_cb:
            self._on_value_change_cb(new_value)

    async def stop_notify(self):
        if not self.bus:
           print("🛑 connection not established, can't stop notify ..")
           await self._publish_status('notify','error', f"❌ connection not established")
           return

        if not self.read_char_iface:
           print("🛑 no read interface, can't stop notify ..")
           await self._publish_status('notify','error', f"❌ no read interface, can't stop notify")
           return

        try:
           if self.read_props_iface:
               try:
                   # Try to remove the callback handler
                   self.read_props_iface.off_properties_changed(self._on_props_changed)
               except AttributeError:
                   pass
               except Exception as e:
                   pass

           await self.read_char_iface.call_stop_notify()

           print("🛑 Notify gestoppt")
           await self._publish_status('disconnect','info', "unsubscribe from messages ..")

        except DBusError as e:
            if "No notify session started" in str(e):
                if has_console:
                   print("ℹ️ Keine Notify-Session – ignoriert")
            else:
                raise

    async def send_hello(self):
        if not self.bus:
           print("🛑 connection not established, can't send hello ..")
           await self._publish_status('send hello','error', f"❌ connection not established")
           return

        connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        if not connected:
           print("🛑 connection lost, can't send ..")
           await self._publish_status('send hello','error', f"❌ connection lost")

           await self.disconnect()
           await self.close()
           return

        if self.write_char_iface:
            await self.write_char_iface.call_write_value(self.hello_bytes, {})
            await self._publish_status('conf load','info', ".. waking up device ..")
            if has_console:
               print(f"📨 Hello sent ..")

        else:
            print("⚠️ Keine Write-Charakteristik verfügbar")

    async def send_message(self, msg, grp):
        if not self.bus:
           print("🛑 connection not established, can't send ..")
           await self._publish_status('send message','error', f"❌ connection not established")
           return

        connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        if not connected:
           print("🛑 connection lost, can't send ..")
           await self._publish_status('send message','error', f"❌ connection lost")

           await self.disconnect()
           await self.close()
           return

        message = "{" + grp + "}" + msg
        byte_array = bytearray(message.encode('utf-8'))

        laenge = len(byte_array) + 2

        byte_array = laenge.to_bytes(1, 'big') +  bytes ([0xA0]) + byte_array

        if self.write_char_iface:
            try:
              await asyncio.wait_for(self.write_char_iface.call_write_value(byte_array, {}), timeout=5)
            except asyncio.TimeoutError:
              print("🕓 Timeout beim Schreiben an BLE-Device")
              await self._publish_status('send message','error', f"❌ Timeout on write")
            except Exception as e:
              print(f"💥 Fehler beim Schreiben an BLE: {e}")
              await self._publish_status('send message','error', f"❌ BLE write error {e}")
        else:
            print("⚠️ Keine Write-Charakteristik verfügbar")

    async def a0_commands(self, cmd):
        if not self.bus:
           print("🛑 connection not established, can't send ..")
           await self._publish_status('a0 command','error', f"❌ connection not established")
           return

        await self._check_conn()

        byte_array = bytearray(cmd.encode('utf-8'))

        laenge = len(byte_array) + 2

        byte_array = laenge.to_bytes(1, 'big') +  bytes ([0xA0]) + byte_array

        if self.write_char_iface:
            await self.write_char_iface.call_write_value(byte_array, {})
            if has_console:
               print(f"📨 Message sent .. {byte_array}")

        else:
            print("⚠️ Keine Write-Charakteristik verfügbar")

    async def set_commands(self, cmd):
       laenge = 0
       
       if not self.bus:
          await self._publish_status('set command','error', f"❌ connection not established")
          print("🛑 connection not established, can't send ..")
          return

       await self._check_conn()

       if has_console:
          print(f"✅ ready to send")

       #ID = 0x20 Timestamp from phone [4B]
       if cmd == "--settime":
         cmd_byte = bytes([0x20])

         now = int(time.time())  # current time in seconds 
         byte_array = now.to_bytes(4, byteorder='little')

         laenge = len(byte_array) + 2
         byte_array = laenge.to_bytes(1, 'big') +  cmd_byte + byte_array 

         if has_console:
            print(f"Aktuelle Zeit {now}")
            print("to hex:", ' '.join(f"{b:02X}" for b in byte_array))
        
       else:
          print(f"❌ {cmd} not yet implemented")

       if self.write_char_iface:
            await self.write_char_iface.call_write_value(byte_array, {})
            if has_console:
               print(f"alles zusammen und raus damit {cmd_byte} {laenge}")
               print(f"📨 Message sent .. {byte_array}")

       else:
            print("⚠️ Keine Write-Charakteristik verfügbar")

    async def _check_conn(self):
        connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        if not connected:
           print(f"⚠️ Verbindung verloren")
           await self.stop_notify()
           await self.dev_iface.call_disconnect()
           await self.close()

           await ble_connect(self.mac)

    async def _send_keepalive(self):
        try:
            while self._connected:
                await asyncio.sleep(300)  # 5 minutes
                if has_console:
                   print(f"📤 Sending keep-alive to {self.mac}")
                try:
                    props = await self.props_iface.call_get_all(DEVICE_INTERFACE)
                    if not props["ServicesResolved"].value:
                       await self._check_conn()

                    else:
                      await self.a0_commands("--pos info")

                except Exception as e:
                    print(f"⚠️ Fehler beim Senden des Keep-Alive: {e}")
        except asyncio.CancelledError:
            print(f"⛔ Keep-alive für {self.mac} gestoppt")

    async def disconnect(self):
        if not self.dev_iface:
            if has_console:
               print("⬇️  not connected - can't disconnect ..")
            return
        try:
            await self._publish_status('disconnect','info', "⬇️  disconnecting ..")

            if self._time_sync is not None:
                await self._time_sync.stop()
                self._time_sync = None

            if self._keepalive_task:
               self._keepalive_task.cancel()
               try:
                  await self._keepalive_task
               except asyncio.CancelledError:
                   pass
               self._keepalive_task = None

            await self.stop_notify()

            try:
                await asyncio.wait_for(self.dev_iface.call_disconnect(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                pass
        
            await self._publish_status('disconnect','ok', "✅ disconnected")
            print(f"🧹 Disconnected von {self.mac}")


        except DBusError as e:
            await self._publish_status('disconnect','error', f"❌ disconnect error {e}")
            if has_console:
               print(f"⚠️ Disconnect fehlgeschlagen: {e}")


    async def close(self):
        if self._time_sync is not None:
            await self._time_sync.stop()
            self._time_sync = None

        if self.bus:
            await asyncio.sleep(1.0)

            try:
                 self.bus.disconnect()
            except Exception as e:
                 pass

        else:
           return

        self.bus = None
        self._connected = False



    async def _handle_timesync(self, lat, lon):
        """Time sync handler that uses BLE client methods instead of global functions"""
        if has_console:
            print("adjusting time on node ..", lat, lon)
        
        await asyncio.sleep(3)
        now = datetime.utcnow()
        
        if lon == 0 or lat == 0:
            if has_console:
                print("Lon/Lat not set, fallback on Raspberry Pi TZ info")
            # Use local timezone
            offset_sec = time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone
            offset = -offset_sec / 3600
            tz_name = "Local"
        else:
            tz = get_timezone_info(lat, lon)
            offset = tz.get("offset_hours")
            tz_name = tz.get("timezone")

        if has_console:
            print("TZ UTC Offset", offset, "TZ name", tz_name)

        print("Time offset detected, correcting time")
        # Use instance methods instead of global handle_command
        await self.a0_commands(f"--utcoff {offset}")
        await asyncio.sleep(2)
        await self.set_commands("--settime")

    def _should_trigger_time_sync(self, message_dict):
        """Check if this GPS message should trigger time sync"""
        if message_dict.get("TYP") != "G":
            return False
            
        # Check if we have valid coordinates
        lat = message_dict.get("LAT", 0)
        lon = message_dict.get("LON", 0)
        
        if lat == 0 and lon == 0:
            return False
            
        # Check time delta (reuse existing logic)
        node_timestamp = safe_timestamp_from_dict(message_dict)
        if node_timestamp is None:
            return False
            
        time_delta = node_time_checker(node_timestamp, "G")
        return abs(time_delta) > 60  # Same threshold as before

    async def process_gps_message(self, message_dict):
        """Process GPS message and trigger time sync if needed - called from notification_handler"""
        if self._should_trigger_time_sync(message_dict):
            lat = message_dict.get("LAT")
            lon = message_dict.get("LON")
            
            if self._time_sync is not None:
                self._time_sync.trigger(lat, lon)
            else:
                print("Warning: time_sync not initialized")

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

      await self._publish_status('scan BLE', 'info', 'command started')

      if self.bus is None:
          self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
      else:
          print("❌ already connected, no scanning possible ..")
          await self._publish_status('scan BLE result', 'error', "already connected, no scanning possible")

          return

      if has_console:
         print("🔍 Starting native BLE scan via BlueZ... timout =",timeout)
      await self._publish_status('scan BLE', 'info', f'🔍 BLE scan active... timeout = {timeout}')

      path = "/org/bluez/hci0"

      introspection = await self.bus.introspect(BLUEZ_SERVICE_NAME, path)
      device_obj = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, introspection)
      self.adapter = device_obj.get_interface(ADAPTER_INTERFACE)
     
      # Track discovered devices
      self.found_devices = {}
      # Event zur Synchronisation
      found_mc_event = asyncio.Event()

      # Listen to InterfacesAdded signal
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
          connected = props.get("Connected", Variant("b", False)).value
          services_resolved = props.get("ServicesResolved", Variant("b", False)).value
          busy = False
          interfaces[DEVICE_INTERFACE]["Busy"] = Variant("b", busy)
        
          if has_console:
            print(f"💾 Found device: {name} ({addr}, paired={paired}, busy={busy})")

      objects["TYP"] = "blueZknown"
      msg=transform_ble(self._normalize_variant(objects))
      await self._send_to_websocket(msg)

      if has_console:
         print(f"\n✅ Found {device_count} known device(s):")
      await self._publish_status('scan BLE', 'info', f".. found {device_count} known device(s) ..")

      #Handler installieren
      def on_interfaces_added_sync(path, interfaces):
          asyncio.create_task(_interfaces_added(path, interfaces))

      self.obj_mgr_iface.on_interfaces_added(on_interfaces_added_sync)

      # Start discovery
      await self.adapter.call_start_discovery()

      try:
         await asyncio.wait_for(found_mc_event.wait(), timeout)
      except asyncio.TimeoutError:
         print("\n")

      await self.adapter.call_stop_discovery()

      if has_console:
        print(f"\n✅ Scan complete. Not paired {len(self.found_devices)} device(s)")
      await self._publish_status('scan BLE', 'info', f"✅ Scan complete, {len(self.found_devices)} not paired device(s)")

      for path, (name, addr, rssi) in self.found_devices.items():
          if has_console:
             print(f"🔹 {name} | Address: {addr} | RSSI: {rssi}")

      self.found_devices["TYP"] = "blueZunKnown"
      msg=transform_ble(self._normalize_variant(self.found_devices))
      await self._send_to_websocket(msg)

      await self.close()
      await asyncio.sleep(2)


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
       return 0

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
        return

    @method()
    def AuthorizeService(self, device: 'o', uuid: 's'):
        print(f"Authorize service {uuid} for {device}")
        return

    @method()
    def Cancel(self):
        print("Request cancelled")


# Module-level functions

async def ble_pair(mac, BLE_Pin, message_router=None):
    path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"
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
        await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'BLE pair result', 
                'result': 'error', 'msg': f"❌ device not found {mac}: {e}",
                'timestamp': int(time.time() * 1000)
            })
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

        await asyncio.sleep(2)
        if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'ble_pair result',
                'result': 'ok', 'msg': f"✅ Successfully paired {mac}",
                'timestamp': int(time.time() * 1000)
            })

        try:
           await dev_iface.call_disconnect()
           print(f"🔌 Disconnected from {mac} after pairing.")
        except Exception as e:
           print(f"⚠️ Could not disconnect from {mac}: {e}")

    except Exception as e:
        print(f"❌ Failed to pair with {mac}: {e}")
        if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'BLE pair result',
                'result': 'error', 'msg': f"❌ failed to pair {mac}: {e}",
                'timestamp': int(time.time() * 1000)
            })


async def ble_unpair(mac, message_router=None):
    if has_console:
       print(f"🧹 Unpairing {mac} using blueZ ...")

    device_path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"
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
      if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'BLE unpair result',
                'result': 'error', 'msg': f"❌ device {mac}",
                'timestamp': int(time.time() * 1000)
            })
      return
 
    print(f"🧹 Unpaired device {mac}")
    if message_router:
        await message_router.publish('ble', 'ble_status', {
            'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'BLE unpair',
            'result': 'ok', 'msg': f"✅ Unpaired device {mac}",
            'timestamp': int(time.time() * 1000)
        })


async def ble_connect(MAC, message_router=None):
    global client

    if client is None:
        client = BLEClient(
            mac=MAC,
            read_uuid="6e400003-b5a3-f393-e0a9-e50e24dcca9e",
            write_uuid="6e400002-b5a3-f393-e0a9-e50e24dcca9e",
            hello_bytes=b'\x04\x10\x20\x30',
            message_router=message_router
        )

    if not client._connected: 
      await client.connect()

      if client._connected:
        await client.start_notify()
        await client.send_hello()

    else:
      if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 
                'TYP': 'blueZ', 
                'command': 'connect BLE result',
                'result': 'info',
                'msg': "BLE connection already running",
                "timestamp": int(time.time() * 1000)
            })

      if has_console:
         print("can't connect, already connected")


async def ble_disconnect(message_router=None):
    global client
    if client is None:
      return
    
    if client._connected: 
      await client.disconnect()
      await client.close()
      client = None
    else:
      if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'disconnect BLE result',
                'result': 'error', 'msg': "can't disconnect, already disconnected",
                'timestamp': int(time.time() * 1000)
            })

      if has_console:
         print("❌ can't disconnect, already disconnected")


async def scan_ble_devices(message_router=None):
    scanclient = BLEClient(
        mac ="",
        read_uuid = "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
        write_uuid = "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
        hello_bytes = b'\x04\x10\x20\x30',
        message_router=message_router
    )
    await scanclient.scan_ble_devices()


async def backend_resolve_ip(hostname, message_router=None):
    import socket
    loop = asyncio.get_event_loop()

    try:
        infos = await loop.run_in_executor(None, socket.getaddrinfo, hostname, None)
        ip = infos[0][4][0]
        if has_console:
           print(f"Resolved IP: {ip}")
        
        if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': "resolve-ip",
                'result': "ok", 'msg': ip, 'timestamp': int(time.time() * 1000)
            })

    except Exception as e:
        if has_console:
           print(f"Error resolving IP: {e}")
        if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': "resolve-ip",
                'result': "error", 'msg': str(e), 'timestamp': int(time.time() * 1000)
            })


# Functions to access the global client
def get_ble_client():
    """Get the current BLE client instance"""
    return client

async def handle_ble_message(msg, grp):
    """Handle messages through global client"""
    global client
    if client is not None:
        await client.send_message(msg, grp)
    else:
        print("BLE client not connected")


async def handle_a0_command(command):
    """Handle A0 commands through global client"""
    global client
    if client is not None:
        await client.a0_commands(command)
    else:
        print("BLE client not connected")


async def handle_set_command(command):
    """Handle set commands through global client"""
    global client
    if client is not None:
        await client.set_commands(command)
    else:
        print("BLE client not connected")
