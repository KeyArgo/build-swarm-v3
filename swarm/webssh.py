"""
WebSocket SSH Bridge for Build Swarm v4.

Provides browser-based terminal access to drones via WebSocket.
Uses paramiko for SSH and the stdlib http.server for WebSocket upgrade.

Note: This is a simplified implementation. For production, consider
using asyncio + websockets or tornado for better performance.

v4.0: Initial implementation
"""

import base64
import hashlib
import json
import logging
import os
import select
import socket
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

log = logging.getLogger('swarm-v3')

# WebSocket opcodes
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


class WebSocketConnection:
    """Simple WebSocket connection handler."""

    def __init__(self, socket_fd, address):
        self.socket = socket_fd
        self.address = address
        self.closed = False

    def recv_frame(self) -> tuple:
        """Receive a WebSocket frame. Returns (opcode, payload)."""
        try:
            # Read first two bytes
            header = self.socket.recv(2)
            if len(header) < 2:
                return None, None

            fin = (header[0] >> 7) & 1
            opcode = header[0] & 0xF
            masked = (header[1] >> 7) & 1
            payload_len = header[1] & 0x7F

            # Extended payload length
            if payload_len == 126:
                ext = self.socket.recv(2)
                payload_len = struct.unpack('>H', ext)[0]
            elif payload_len == 127:
                ext = self.socket.recv(8)
                payload_len = struct.unpack('>Q', ext)[0]

            # Masking key (if masked)
            mask = b''
            if masked:
                mask = self.socket.recv(4)

            # Payload
            payload = b''
            remaining = payload_len
            while remaining > 0:
                chunk = self.socket.recv(min(remaining, 4096))
                if not chunk:
                    break
                payload += chunk
                remaining -= len(chunk)

            # Unmask if needed
            if masked and mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            return opcode, payload

        except Exception as e:
            log.debug(f"WebSocket recv error: {e}")
            return None, None

    def send_frame(self, opcode: int, payload: bytes):
        """Send a WebSocket frame."""
        if self.closed:
            return

        try:
            frame = bytearray()

            # First byte: FIN + opcode
            frame.append(0x80 | opcode)

            # Second byte: length (server frames are not masked)
            length = len(payload)
            if length <= 125:
                frame.append(length)
            elif length <= 65535:
                frame.append(126)
                frame.extend(struct.pack('>H', length))
            else:
                frame.append(127)
                frame.extend(struct.pack('>Q', length))

            # Payload
            frame.extend(payload)

            self.socket.sendall(bytes(frame))

        except Exception as e:
            log.debug(f"WebSocket send error: {e}")
            self.closed = True

    def send_text(self, text: str):
        """Send a text message."""
        self.send_frame(OPCODE_TEXT, text.encode('utf-8'))

    def send_binary(self, data: bytes):
        """Send binary data."""
        self.send_frame(OPCODE_BINARY, data)

    def close(self, code: int = 1000, reason: str = ''):
        """Close the WebSocket connection."""
        if self.closed:
            return
        payload = struct.pack('>H', code) + reason.encode('utf-8')
        self.send_frame(OPCODE_CLOSE, payload)
        self.closed = True


def compute_accept_key(key: str) -> str:
    """Compute the Sec-WebSocket-Accept header value."""
    magic = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
    accept = base64.b64encode(
        hashlib.sha1((key + magic).encode()).digest()
    ).decode()
    return accept


class SSHSession:
    """SSH session to a drone."""

    def __init__(self, host: str, user: str = 'root', port: int = 22,
                 key_path: str = None):
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.process: Optional[subprocess.Popen] = None
        self.closed = False

    def connect(self) -> bool:
        """Start an SSH process with PTY."""
        try:
            cmd = [
                'ssh',
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'UserKnownHostsFile=/dev/null',
                '-o', 'LogLevel=ERROR',
                '-tt',  # Force PTY allocation
            ]

            if self.port != 22:
                cmd.extend(['-p', str(self.port)])

            if self.key_path and os.path.exists(self.key_path):
                cmd.extend(['-i', self.key_path])

            cmd.append(f'{self.user}@{self.host}')

            # Start SSH with PTY via script command for proper terminal emulation
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )

            log.info(f"[SSH] Connected to {self.user}@{self.host}:{self.port}")
            return True

        except Exception as e:
            log.error(f"[SSH] Connection failed: {e}")
            return False

    def send(self, data: bytes):
        """Send data to the SSH process."""
        if self.process and self.process.stdin:
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except Exception as e:
                log.debug(f"[SSH] Send error: {e}")

    def recv(self, timeout: float = 0.1) -> bytes:
        """Receive data from the SSH process (non-blocking)."""
        if not self.process or not self.process.stdout:
            return b''

        try:
            # Use select for non-blocking read
            rlist, _, _ = select.select([self.process.stdout], [], [], timeout)
            if rlist:
                # Read available data
                data = os.read(self.process.stdout.fileno(), 4096)
                return data
        except Exception as e:
            log.debug(f"[SSH] Recv error: {e}")

        return b''

    def close(self):
        """Close the SSH session."""
        if self.closed:
            return
        self.closed = True

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass

        log.info(f"[SSH] Disconnected from {self.host}")


class WebSSHBridge:
    """Bridge between WebSocket and SSH for browser terminal access."""

    def __init__(self, db):
        self.db = db
        self.sessions = {}  # ws_id -> (ws_conn, ssh_session)
        self._lock = threading.Lock()

    def handle_upgrade(self, handler: BaseHTTPRequestHandler) -> bool:
        """Handle WebSocket upgrade request. Returns True if upgraded."""
        # Check for WebSocket upgrade headers
        upgrade = handler.headers.get('Upgrade', '').lower()
        connection = handler.headers.get('Connection', '').lower()

        if 'websocket' not in upgrade or 'upgrade' not in connection:
            return False

        ws_key = handler.headers.get('Sec-WebSocket-Key')
        if not ws_key:
            handler.send_error(400, 'Missing Sec-WebSocket-Key')
            return True

        # Extract drone name from path: /ws/ssh/<drone_name>
        path_parts = handler.path.split('/')
        if len(path_parts) < 4 or path_parts[2] != 'ssh':
            handler.send_error(404, 'Invalid WebSocket path')
            return True

        drone_name = path_parts[3].split('?')[0]

        # Look up drone
        node = self.db.get_node_by_name(drone_name)
        if not node:
            handler.send_error(404, f'Drone not found: {drone_name}')
            return True

        # Get SSH config
        ip = node.get('tailscale_ip') or node.get('ip')
        if not ip:
            handler.send_error(400, 'Drone has no IP address')
            return True

        ssh_cfg = self.db.fetchone(
            "SELECT ssh_user, ssh_port, ssh_key_path FROM drone_config WHERE node_name = ?",
            (drone_name,))

        user = 'root'
        port = 22
        key_path = None

        if ssh_cfg:
            user = ssh_cfg['ssh_user'] or 'root'
            port = ssh_cfg['ssh_port'] or 22
            key_path = ssh_cfg['ssh_key_path']

        # Send WebSocket upgrade response
        accept = compute_accept_key(ws_key)
        response = (
            'HTTP/1.1 101 Switching Protocols\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Accept: {accept}\r\n'
            '\r\n'
        )
        handler.wfile.write(response.encode())
        handler.wfile.flush()

        # Create WebSocket connection wrapper
        ws = WebSocketConnection(handler.connection, handler.client_address)

        # Create SSH session
        ssh = SSHSession(ip, user, port, key_path)
        if not ssh.connect():
            ws.send_text(json.dumps({'error': 'SSH connection failed'}))
            ws.close(1011, 'SSH connection failed')
            return True

        # Send welcome message
        ws.send_text(json.dumps({
            'type': 'connected',
            'drone': drone_name,
            'ip': ip,
            'user': user,
        }))

        # Start relay threads
        session_id = f"{drone_name}-{time.time()}"
        with self._lock:
            self.sessions[session_id] = (ws, ssh)

        try:
            self._relay_loop(ws, ssh, session_id)
        finally:
            with self._lock:
                self.sessions.pop(session_id, None)
            ssh.close()
            ws.close()

        return True

    def _relay_loop(self, ws: WebSocketConnection, ssh: SSHSession, session_id: str):
        """Relay data between WebSocket and SSH."""
        log.info(f"[WebSSH] Relay started for {session_id}")

        # Thread to read from SSH and send to WebSocket
        def ssh_to_ws():
            while not ws.closed and not ssh.closed:
                data = ssh.recv(0.05)
                if data:
                    try:
                        # Try to decode as UTF-8, fall back to latin-1
                        text = data.decode('utf-8', errors='replace')
                        ws.send_text(json.dumps({'type': 'output', 'data': text}))
                    except Exception as e:
                        log.debug(f"[WebSSH] SSH→WS error: {e}")
                        break

        ssh_thread = threading.Thread(target=ssh_to_ws, daemon=True)
        ssh_thread.start()

        # Main loop: read from WebSocket and send to SSH
        try:
            while not ws.closed:
                opcode, payload = ws.recv_frame()

                if opcode is None:
                    break

                if opcode == OPCODE_CLOSE:
                    log.info(f"[WebSSH] Client closed connection: {session_id}")
                    break

                if opcode == OPCODE_PING:
                    ws.send_frame(OPCODE_PONG, payload)
                    continue

                if opcode == OPCODE_TEXT:
                    try:
                        msg = json.loads(payload.decode('utf-8'))
                        msg_type = msg.get('type', 'input')

                        if msg_type == 'input':
                            data = msg.get('data', '')
                            ssh.send(data.encode('utf-8'))

                        elif msg_type == 'resize':
                            # Terminal resize (not implemented for subprocess-based SSH)
                            pass

                    except json.JSONDecodeError:
                        # Raw text input
                        ssh.send(payload)

                elif opcode == OPCODE_BINARY:
                    ssh.send(payload)

        except Exception as e:
            log.error(f"[WebSSH] Relay error: {e}")

        ssh.closed = True
        log.info(f"[WebSSH] Relay ended for {session_id}")


# Singleton instance
_bridge: Optional[WebSSHBridge] = None


def init_webssh(db) -> WebSSHBridge:
    """Initialize the WebSSH bridge."""
    global _bridge
    _bridge = WebSSHBridge(db)
    return _bridge


def get_bridge() -> Optional[WebSSHBridge]:
    """Get the WebSSH bridge instance."""
    return _bridge
