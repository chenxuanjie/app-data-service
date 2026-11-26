import asyncio
import json
import logging
import re
from contextlib import suppress
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from time import monotonic, time
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

HEARTBEAT_TIMEOUT = 15
HELLO_TIMEOUT = 5
SEND_TIMEOUT = 5
QUEUE_SIZE = 32
VEHICLE_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}")
NETWORK_MODES = {"sta", "ap", "sta_ap"}
logger = logging.getLogger("uvicorn.error")


class Peer:
    def __init__(self, socket):
        self.socket = socket
        self.outgoing = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.closed = asyncio.Event()

    def send(self, frame):
        if self.closed.is_set():
            return
        try:
            self.outgoing.put_nowait(frame)
        except asyncio.QueueFull:
            self.closed.set()

    async def write(self):
        while True:
            frame = await self.outgoing.get()
            await asyncio.wait_for(self.socket.send_json(frame), SEND_TIMEOUT)

    async def run(self, receive):
        tasks = [
            asyncio.create_task(receive()),
            asyncio.create_task(self.write()),
            asyncio.create_task(self.closed.wait()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, TimeoutError, TypeError, ValueError):
            pass
        finally:
            self.closed.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await close_socket(self.socket)


async def close_socket(socket, code=1000, reason=""):
    with suppress(WebSocketDisconnect, RuntimeError, TimeoutError):
        await asyncio.wait_for(socket.close(code, reason), SEND_TIMEOUT)


def read_network(frame):
    network = frame.get("network")
    if not isinstance(network, dict):
        raise TypeError("expected a network object")
    mode = network.get("mode")
    if mode is not None and (not isinstance(mode, str) or mode not in NETWORK_MODES):
        raise ValueError("invalid network mode")
    lan_ip = network.get("lan_ip")
    if lan_ip is not None:
        if not isinstance(lan_ip, str) or mode not in {"sta", "sta_ap"}:
            raise ValueError("invalid STA address")
        lan_ip = str(IPv4Address(lan_ip))
    return mode, lan_ip


@dataclass
class Vehicle:
    vehicle_id: str
    agent: Peer | None
    network_mode: str | None
    lan_ip: str | None
    last_seen: int = field(default_factory=lambda: int(time()))
    deadline: float = field(default_factory=lambda: monotonic() + HEARTBEAT_TIMEOUT)
    clients: dict[str, Peer] = field(default_factory=dict)

    @property
    def online(self):
        return (
            self.agent is not None
            and not self.agent.closed.is_set()
            and monotonic() < self.deadline
        )

    def heartbeat(self, frame):
        self.network_mode, self.lan_ip = read_network(frame)
        self.last_seen = int(time())
        self.deadline = monotonic() + HEARTBEAT_TIMEOUT

    def disconnect(self):
        if self.agent is not None:
            self.agent.closed.set()
            self.agent = None
        for client in self.clients.values():
            client.closed.set()
        self.clients.clear()

    def snapshot(self):
        return {
            "vehicle_id": self.vehicle_id,
            "status": "online" if self.online else "offline",
            "network_mode": self.network_mode,
            "lan_ip": self.lan_ip,
            "last_seen": self.last_seen,
        }


async def read_object(socket, timeout=None):
    event = await asyncio.wait_for(socket.receive(), timeout)
    if event["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(event["code"])
    if event.get("text") is None:
        raise ValueError("expected a text frame")
    frame = json.loads(event["text"])
    if not isinstance(frame, dict):
        raise TypeError("expected a JSON object")
    return frame


def build_router():
    router = APIRouter()
    vehicles = {}

    @router.get("/v1/vehicles")
    async def list_vehicles():
        return JSONResponse(
            {"vehicles": [vehicles[key].snapshot() for key in sorted(vehicles)]},
            headers={"Cache-Control": "no-store"},
        )

    @router.websocket("/v1/agent/ws")
    async def agent_socket(socket: WebSocket):
        await socket.accept()
        try:
            hello = await read_object(socket, HELLO_TIMEOUT)
            vehicle_id = hello.get("vehicle_id")
            if (
                hello.get("type") != "agent.hello"
                or not isinstance(vehicle_id, str)
                or VEHICLE_ID.fullmatch(vehicle_id) is None
            ):
                raise ValueError("invalid vehicle hello")
            mode, lan_ip = read_network(hello)
        except (TimeoutError, TypeError, ValueError):
            await close_socket(socket, 1008, "invalid_hello")
            return
        except WebSocketDisconnect:
            return

        previous = vehicles.get(vehicle_id)
        if previous is not None and previous.online:
            logger.warning("Duplicate vehicle ID: %s", vehicle_id)
            await close_socket(socket, 1008, "duplicate_vehicle_id")
            return
        if previous is not None:
            previous.disconnect()
        agent = Peer(socket)
        vehicle = vehicles[vehicle_id] = Vehicle(vehicle_id, agent, mode, lan_ip)

        async def receive():
            while vehicle.online:
                frame = await read_object(socket, vehicle.deadline - monotonic())
                if frame.get("type") == "agent.heartbeat":
                    vehicle.heartbeat(frame)
                elif frame.get("type") == "relay.message":
                    client_id = frame.get("client_id")
                    payload = frame.get("payload")
                    if isinstance(client_id, str) and isinstance(payload, dict):
                        client = vehicle.clients.get(client_id)
                        if client is not None:
                            client.send(payload)

        # Release this session's clients before awaiting the socket close handshake.
        async def receive_until_closed():
            try:
                await receive()
            finally:
                vehicle.disconnect()

        try:
            await agent.run(receive_until_closed)
        finally:
            vehicle.disconnect()

    @router.websocket("/v1/client/ws")
    async def client_socket(socket: WebSocket):
        await socket.accept()
        vehicle = vehicles.get(socket.query_params.get("vehicle_id"))
        if vehicle is None or not vehicle.online:
            await close_socket(socket, 1013, "vehicle_offline")
            return

        client_id = uuid4().hex
        client = vehicle.clients[client_id] = Peer(socket)
        agent = vehicle.agent

        async def receive():
            while vehicle.online:
                payload = await read_object(socket)
                agent.send(
                    {
                        "type": "relay.message",
                        "client_id": client_id,
                        "payload": payload,
                    }
                )

        try:
            await client.run(receive)
        finally:
            if vehicle.clients.pop(client_id, None) is not None:
                agent.send({"type": "relay.close", "client_id": client_id})

    return router
