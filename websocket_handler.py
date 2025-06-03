#!/usr/bin/env python3
import asyncio
import json
import time
import websockets

VERSION="v0.37.0"


class WebSocketManager:
    def __init__(self, host, port, message_router=None):
        self.host = host
        self.port = port
        self.message_router = message_router
        self.clients = set()
        self.clients_lock = asyncio.Lock()
        self.server = None
        
        # Subscribe to messages we want to broadcast to WebSocket clients
        if message_router:
            message_router.subscribe('mesh_message', self._broadcast_handler)
            message_router.subscribe('websocket_message', self._broadcast_handler)
            message_router.subscribe('ble_notification', self._broadcast_handler)
            message_router.subscribe('ble_status', self._broadcast_handler)
            message_router.subscribe('websocket_direct', self._direct_send_handler)
            
            
    async def _direct_send_handler(self, routed_message):
        """Handle direct WebSocket sends to specific clients"""
        message_data = routed_message['data']
        websocket = message_data.get('websocket')
        data = message_data.get('data')
        
        if websocket and data:
            try:
                json_message = json.dumps(data)
                await websocket.send(json_message)
                print(f"📡 WebSocketManager: Direct send to client successful")
            except Exception as e:
                print(f"📡 WebSocketManager: Direct send failed: {e}")
        else:
            print(f"📡 WebSocketManager: Invalid direct send data: {message_data}")

    async def _broadcast_handler(self, routed_message):
        """Handle messages from the router and broadcast to WebSocket clients"""
        # Extract the actual message data
        message_data = routed_message['data']
        await self.broadcast_message(message_data)
        
        #msg_preview = str(message_data).get('msg', str(message_data))[:50]
        #msg_preview = message_data.get('msg', str(message_data))[:50]
    
        #print(f"📡 WebSocketManager: Broadcasted {routed_message['type']} from {routed_message['source']}: {msg_preview}...")
        print(f"📡 WebSocketManager: Broadcasted {routed_message['type']} from {routed_message['source']}: {message_data}...")
            
    async def broadcast_message(self, message):
        """Broadcast message to all connected WebSocket clients"""
        async with self.clients_lock:
            targets = list(self.clients)
        
        if targets:
            json_message = json.dumps(message)
            send_tasks = [asyncio.create_task(client.send(json_message)) for client in targets]
            results = await asyncio.gather(*send_tasks, return_exceptions=True)
            
            # Count successful sends
            successful = sum(1 for r in results if not isinstance(r, Exception))
            #print(f"📡 WebSocketManager: Sent to {successful}/{len(targets)} clients")
        
    async def start_server(self):
        """Start the WebSocket server"""
        self.server = await websockets.serve(self._handle_connection, self.host, self.port)
        print(f"📡 WebSocketManager: Server started on {self.host}:{self.port}")
        
    async def stop_server(self):
        """Stop the WebSocket server and disconnect all clients"""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            
        async with self.clients_lock:
            clients_to_close = list(self.clients)
            
        for client in clients_to_close:
            try:
                await client.close()
            except:
                pass
                
        print("📡 WebSocketManager: Server stopped")
        
    async def _handle_connection(self, websocket):
        """Handle individual WebSocket client connections"""
        peer = websocket.remote_address[0] if websocket.remote_address else "unknown"
        print(f"📡 WebSocketManager: Client connected from {peer}")
        
        async with self.clients_lock:
            self.clients.add(websocket)
            
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    print(f"📡 WebSocketManager: Received from {peer}: {data}")
                        
                    await self._process_client_message(data, websocket, peer)
                    
                except json.JSONDecodeError:
                    print(f"📡 WebSocketManager: Invalid JSON from {peer}: {message}")
                    
        except websockets.exceptions.ConnectionClosed as e:
            print(f"📡 WebSocketManager: {peer} disconnected: {e.code} - {e.reason}")
        except Exception as e:
            print(f"📡 WebSocketManager: Error with {peer}: {e}")
        finally:
            print(f"📡 WebSocketManager: Cleaning up connection from {peer}")
            async with self.clients_lock:
                self.clients.discard(websocket)
                
    async def _process_client_message(self, data, websocket, peer):
        """Process messages received from WebSocket clients"""
        message_type = data.get("type")
        
        if message_type == "command":
            # Route command through message router
            if self.message_router:
                await self.message_router.route_command(
                    data.get("msg"), 
                    websocket=websocket,
                    MAC=data.get("MAC"),
                    BLE_Pin=data.get("BLE_Pin")
                )
                
        elif message_type == "BLE":
            # Publish BLE message to router
            if self.message_router:
                await self.message_router.publish('websocket', 'ble_message', {
                    'msg': data.get("msg"),
                    'dst': data.get("dst")
                })
                    
        else:
            # Publish UDP message to router
            if self.message_router:
                await self.message_router.publish('websocket', 'udp_message', data)
                
    def get_client_count(self):
        """Return number of connected clients"""
        return len(self.clients)
