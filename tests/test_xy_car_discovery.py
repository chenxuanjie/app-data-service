import json
import socket
import time
import unittest
from threading import Thread
from unittest.mock import patch
from urllib.request import urlopen

import pytest
import uvicorn
from test_sessions import authenticated as authenticated
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from app_data_service.modules.xy_car_app import discovery as vehicle_discovery

AGENT = "/v1/agent/ws"
CLIENT = "/v1/client/ws?vehicle_id="
VEHICLES = "/v1/vehicles"


def send(connection, frame):
    connection.send(json.dumps(frame))


def receive(connection):
    return json.loads(connection.recv(timeout=2))


def hello(socket, vehicle_id, ip):
    send(
        socket,
        {
            "type": "agent.hello",
            "vehicle_id": vehicle_id,
            "network": {"mode": "sta", "lan_ip": ip},
        },
    )


class VehicleDiscoveryTest(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def integrated_app(self, authenticated):
        client, *_ = authenticated
        self.app = client.app

    def setUp(self):
        listener = self.enterContext(socket.socket())
        listener.bind(("127.0.0.1", 0))
        self.port = listener.getsockname()[1]
        config = uvicorn.Config(self.app, ws="websockets-sansio", log_level="warning")
        self.server = uvicorn.Server(config)
        worker = Thread(target=self.server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        self.addCleanup(self.stop, worker)
        deadline = time.monotonic() + 3
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started)

    def stop(self, worker):
        self.server.should_exit = True
        worker.join(3)
        self.assertFalse(worker.is_alive())

    def websocket(self, path):
        return connect(
            f"ws://127.0.0.1:{self.port}{path}", open_timeout=2, close_timeout=1, proxy=None
        )

    def list_vehicles(self):
        with urlopen(f"http://127.0.0.1:{self.port}{VEHICLES}", timeout=2) as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return json.load(response)["vehicles"]

    def vehicles_when(self, ready):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            vehicles = {row["vehicle_id"]: row for row in self.list_vehicles()}
            if ready(vehicles):
                return vehicles
            time.sleep(0.01)
        self.fail(f"Unexpected vehicle state: {vehicles}")

    def test_reports_change_ip_and_reject_duplicate_vehicle(self):
        with self.websocket(AGENT) as first, self.websocket(AGENT) as second:
            hello(first, "xycar_01", "192.168.1.21")
            hello(second, "xycar_02", "192.168.1.22")
            self.vehicles_when(
                lambda rows: (
                    len(rows) == 2 and all(row["status"] == "online" for row in rows.values())
                )
            )

            with self.websocket(AGENT) as duplicate:
                hello(duplicate, "xycar_01", "192.168.1.99")
                with self.assertRaises(ConnectionClosed) as rejection:
                    receive(duplicate)
                self.assertEqual(rejection.exception.rcvd.reason, "duplicate_vehicle_id")

            send(
                first,
                {
                    "type": "agent.heartbeat",
                    "network": {"mode": "sta_ap", "lan_ip": "192.168.1.33"},
                },
            )
            rows = self.vehicles_when(lambda rows: rows["xycar_01"]["lan_ip"] == "192.168.1.33")
            self.assertEqual(rows["xycar_01"]["network_mode"], "sta_ap")
            self.assertEqual(rows["xycar_02"]["lan_ip"], "192.168.1.22")
        self.vehicles_when(lambda rows: all(row["status"] == "offline" for row in rows.values()))

    def test_relay_keeps_each_vehicle_and_client_session_separate(self):
        with self.websocket(AGENT) as first, self.websocket(AGENT) as second:
            hello(first, "xycar_01", "192.168.1.21")
            hello(second, "xycar_02", "192.168.1.22")
            self.vehicles_when(lambda rows: len(rows) == 2)
            with (
                self.websocket(CLIENT + "xycar_01") as first_client,
                self.websocket(CLIENT + "xycar_02") as second_client,
            ):
                for vehicle, agent, client in [
                    ("xycar_01", first, first_client),
                    ("xycar_02", second, second_client),
                ]:
                    send(client, {"v": 1, "type": "ping", "id": "rtt-1"})
                    request = receive(agent)
                    self.assertEqual(request["payload"]["id"], "rtt-1")
                    pong = {"type": "pong", "id": "rtt-1", "vehicle_id": vehicle}
                    send(
                        agent,
                        {
                            "type": "relay.message",
                            "client_id": request["client_id"],
                            "payload": pong,
                        },
                    )
                    self.assertEqual(receive(client), pong)
            self.assertEqual(receive(first)["type"], "relay.close")
            self.assertEqual(receive(second)["type"], "relay.close")

    def test_heartbeat_timeout_and_reregister(self):
        with (
            patch.object(vehicle_discovery, "HEARTBEAT_TIMEOUT", 0.5),
            self.websocket(AGENT) as expired,
        ):
            hello(expired, "xycar_01", "192.168.1.21")
            self.vehicles_when(lambda rows: rows.get("xycar_01", {}).get("status") == "offline")
            with self.websocket(CLIENT + "xycar_01") as client:
                with self.assertRaises(ConnectionClosed) as rejection:
                    receive(client)
                self.assertEqual(rejection.exception.rcvd.reason, "vehicle_offline")
            with self.websocket(AGENT) as replacement:
                hello(replacement, "xycar_01", "192.168.1.45")
                with self.assertRaises(ConnectionClosed):
                    receive(expired)
                rows = self.vehicles_when(lambda rows: rows["xycar_01"]["status"] == "online")
                self.assertEqual(rows["xycar_01"]["lan_ip"], "192.168.1.45")


if __name__ == "__main__":
    unittest.main()
