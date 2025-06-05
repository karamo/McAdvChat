┌─────────────────────────────┐
                    │     WebSocket Clients       │
                    │   (Vue.js SPA Frontend)     │
                    └─────────────┬───────────────┘
                                  │ WSS:2981
                                  │ (via Caddy proxy)
                                  │
    ┌─────────────────────────────▼─────────────────────────────┐
    │                MESSAGE ROUTER                             │
    │           (Central Hub - C2-mc-ws.py)                     │
    │                                                           │
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐    │
    │  │ UDP Handler │  │ BLE Handler │  │ WebSocket Mgr   │    │
    │  │             │  │             │  │                 │    │
    │  │ Port :1799  │  │ D-Bus/BlueZ │  │ Port :2980      │    │
    │  └─────────────┘  └─────────────┘  └─────────────────┘    │
    │                                                           │
    │  ┌─────────────────────────────────────────────────────┐  │
    │  │         MessageStorageHandler                       │  │
    │  │      (In-memory + JSON persistence)                 │  │
    │  └─────────────────────────────────────────────────────┘  │
    └─────────────┬───────────────┬─────────────────────────────┘
                  │               │
                  │ UDP:1799      │ Bluetooth Classic
                  │               │ (GATT characteristics)
                  ▼               ▼
    ┌─────────────────────┐   ┌─────────────────────┐
    │    MeshCom Node     │   │   ESP32 LoRa Node   │
    │  (192.168.68.69)    │   │    (MC-xxxxxx)      │
    │                     │   │                     │
    │ ┌─────────────────┐ │   │ ┌─────────────────┐ │
    │ │ LoRa Mesh Radio │ │   │ │ LoRa Mesh Radio │ │
    │ │ APRS Decoder    │ │   │ │ APRS Generator  │ │
    │ │ Message Router  │ │   │ │ GPS Module      │ │
    │ └─────────────────┘ │   │ └─────────────────┘ │
    └─────────────────────┘   └─────────────────────┘
                  │                       │
                  └───────────────────────┘
                     433MHz LoRa Mesh
                   (Ham Radio Frequencies)
