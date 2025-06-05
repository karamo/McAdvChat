import asyncio
import hashlib
import json
import os
import secrets
import signal
import subprocess
from pathlib import Path

import websockets
from websockets.server import WebSocketServerProtocol

VERSION="v0.38.0"

CONFIG_PATH = "/etc/mcadvchat/config.json"
PROXY_SCRIPT = "/usr/local/bin/C2-mc-ws.py"
VENV_PYTHON = "/home/martin/venv/bin/python"
PROXY_LOG_PATH = "/tmp/proxy.log"
MAGIC_COMMANDS = {"restart", "status"}


def verify_magic_word(stored_hash: str, attempt: str) -> bool:
    try:
        salt, hashed = stored_hash.split("$", 1)
        check = hashlib.sha256((salt + attempt).encode()).hexdigest()
        return check == hashed
    except Exception:
        return False


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


class ProxySupervisor:
    def __init__(self, config):
        self.proc = None
        self.clients = set()
        self.config = config
        self.magic_hash = self.config.get("MAGIC_WORD_HASH", "")

    async def start_proxy(self):
        if self.proc and self.proc.returncode is None:
            return
        log_file = open(PROXY_LOG_PATH, "w")
        self.proc = await asyncio.create_subprocess_exec(
            VENV_PYTHON,
            PROXY_SCRIPT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._forward_logs_to_journal())

    async def stop_proxy(self):
        if self.proc and self.proc.returncode is None:
            self.proc.send_signal(signal.SIGTERM)
            await self.proc.wait()

    async def restart_proxy(self):
        await self.stop_proxy()
        await self.start_proxy()

    async def _forward_logs_to_journal(self):
        if self.proc.stdout is None:
            return
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            log = line.decode().strip()
            subprocess.run([
                "logger",
                "-t", "mcproxy",
                log
            ])

    async def stream_logs(self, websocket: WebSocketServerProtocol):
        if not os.path.exists(PROXY_LOG_PATH):
            await websocket.send("[supervisor] No logs available.")
            return
        with open(PROXY_LOG_PATH, "r") as f:
            for line in f:
                await websocket.send(f"[log] {line.strip()}")

    def is_authorized(self, message: str) -> bool:
        try:
            cmd, secret = message.split(" ", 1)
            return cmd in MAGIC_COMMANDS and verify_magic_word(self.magic_hash, secret.strip())
        except ValueError:
            return False

    async def broadcast(self, msg: str):
        for ws in self.clients:
            try:
                await ws.send(msg)
            except:
                pass

    async def handle_client(self, websocket: WebSocketServerProtocol):
        self.clients.add(websocket)
        try:
            await websocket.send("[supervisor] Connected.")
            async for message in websocket:
                if message.startswith("restart "):
                    if self.is_authorized(message):
                        await websocket.send("[supervisor] Restarting proxy...")
                        await self.restart_proxy()
                    else:
                        await websocket.send("[supervisor] Unauthorized.")
                elif message.startswith("status "):
                    if self.is_authorized(message):
                        status = "running" if self.proc and self.proc.returncode is None else "stopped"
                        await websocket.send(f"[supervisor] Proxy status: {status}")
                    else:
                        await websocket.send("[supervisor] Unauthorized.")
                elif message == "logs":
                    await self.stream_logs(websocket)
                elif message == "update":
                    await websocket.send("[supervisor] Running update script...")
                    await self.run_update_scripts(websocket)
                else:
                    await websocket.send(f"[supervisor] Unknown command: {message}")
        finally:
            self.clients.remove(websocket)

    async def run_update_scripts(self, websocket):
        try:
            await asyncio.create_subprocess_shell(
                "curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/mc-install.sh | sudo bash"
            )
            await asyncio.create_subprocess_shell(
                "curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/install_mcproxy.sh | bash"
            )
            await websocket.send("[supervisor] Update completed.")
        except Exception as e:
            await websocket.send(f"[supervisor] Update failed: {e}")


class SupervisorServer:
    def __init__(self):
        self.config = load_config()
        self.host = self.config.get("SV_HOST", "127.0.0.1")
        self.port = self.config.get("SV_PORT", 2982)
        self.supervisor = ProxySupervisor(self.config)

    async def run(self):
        await self.supervisor.start_proxy()
        async with websockets.serve(self.supervisor.handle_client, self.host, self.port):
            print(f"[supervisor] Listening on ws://{self.host}:{self.port}")
            await asyncio.Future()  # Run forever


def main():
    server = SupervisorServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()

