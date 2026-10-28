"""Task-owned loopback relay when WSL's Windows localhost forwarding is absent."""

import socket
import socketserver
import subprocess
import threading
from contextlib import contextmanager

REMOTE = """
import socket,sys,threading
with socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=15) as upstream:
    upstream.settimeout(7200)
    def upload():
        try:
            while chunk := sys.stdin.buffer.read1(65536): upstream.sendall(chunk)
        except OSError: pass
        finally:
            try: upstream.shutdown(socket.SHUT_WR)
            except OSError: pass
    threading.Thread(target=upload,daemon=True).start()
    while chunk := upstream.recv(65536):
        sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()
"""


class Relay(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, command):
        self.command = command
        self.lock = threading.Lock()
        self.sessions = set()
        self.closing = False
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def close_sessions(self):
        with self.lock:
            self.closing = True
            for connection, child in self.sessions:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                if child.poll() is None:
                    child.terminate()


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            child = subprocess.Popen(
                self.server.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return
        session = (self.request, child)
        with self.server.lock:
            if self.server.closing:
                child.terminate()
                child.wait(timeout=5)
                child.stdin.close()
                child.stdout.close()
                return
            self.server.sessions.add(session)
        self.request.settimeout(7200)

        def upload():
            try:
                while chunk := self.request.recv(65536):
                    child.stdin.write(chunk)
                    child.stdin.flush()
            except (OSError, ValueError):
                pass
            finally:
                child.stdin.close()

        worker = threading.Thread(target=upload, daemon=True)
        worker.start()
        try:
            while chunk := child.stdout.read1(65536):
                self.request.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            worker.join(timeout=2)
            child.stdout.close()
            with self.server.lock:
                self.server.sessions.discard(session)


@contextmanager
def database_transport(host, port):
    if (host, port) != ("127.0.0.1", 55432):
        raise RuntimeError("Local database relay refuses an unexpected endpoint")
    try:
        connection = socket.create_connection((host, port), timeout=3)
    except OSError:
        connection = None
    if connection is not None:
        connection.close()
        print("READY native Windows to WSL database forwarding", flush=True)
        yield
        return
    command = ["wsl", "-d", "Ubuntu", "--exec", "python3", "-u", "-c", REMOTE, str(port)]
    try:
        server = Relay((host, port), command)
    except OSError as error:
        raise RuntimeError(
            "WINDOWS_WSL_FORWARDING_FAILED: WSL PostgreSQL is ready, but Windows "
            "port 55432 is unavailable or occupied. "
            "No process was killed and no WSL reset attempted."
        ) from error
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    print(
        "RECOVER task-owned 127.0.0.1:55432 relay to the same WSL PostgreSQL over stdio", flush=True
    )
    try:
        yield
    finally:
        server.shutdown()
        server.close_sessions()
        server.server_close()
        worker.join(timeout=5)
