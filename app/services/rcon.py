# app/services/rcon.py
"""
Minecraft RCON Protocol Client

Handles:
- RCON connection and authentication
- Command execution
- Server properties parsing
- Minecraft color code stripping
"""

import logging
import re
import socket
import struct
from dataclasses import dataclass
from typing import Optional

from app.services import minecraft_settings

logger = logging.getLogger(__name__)

# Legacy alias. Runtime reads use minecraft_settings so admin changes apply
# without importing this module again.
SERVER_PROPERTIES = minecraft_settings.get_server_properties_path()


def strip_minecraft_colors(text: str) -> str:
    """Strip Minecraft color/formatting codes (§X) from text"""
    # § followed by any character (0-9, a-f, k-o, r, x for hex, etc.)
    return re.sub(r'§.', '', text)


def load_server_properties() -> dict:
    """Load server.properties file"""
    props = {}
    server_properties = minecraft_settings.get_server_properties_path()
    if server_properties.exists():
        with open(server_properties, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    props[key.strip()] = value.strip()
    return props


@dataclass
class RCONConfig:
    """RCON configuration"""
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 25575
    password: str = ""


def get_rcon_config() -> RCONConfig:
    """Get RCON configuration from server.properties"""
    props = load_server_properties()
    return RCONConfig(
        enabled=props.get("enable-rcon", "false").lower() == "true",
        host="127.0.0.1",
        port=int(props.get("rcon.port", "25575")),
        password=props.get("rcon.password", "")
    )


class RCONClient:
    """Minecraft RCON protocol client"""

    SERVERDATA_AUTH = 3
    SERVERDATA_AUTH_RESPONSE = 2
    SERVERDATA_EXECCOMMAND = 2
    SERVERDATA_RESPONSE_VALUE = 0
    MAX_PACKET_SIZE = 4096  # Standard RCON max packet size

    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self.socket: Optional[socket.socket] = None
        self.request_id = 0

    def __enter__(self):
        """Context manager entry - connect and authenticate"""
        if self.connect():
            return self
        raise ConnectionError(f"Failed to connect to RCON at {self.host}:{self.port}")

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - disconnect"""
        self.disconnect()
        return False

    def _pack_packet(self, packet_type: int, payload: str) -> bytes:
        """Pack a packet for sending"""
        self.request_id += 1
        payload_bytes = payload.encode("utf-8") + b"\x00\x00"
        length = 4 + 4 + len(payload_bytes)
        return struct.pack("<iii", length, self.request_id, packet_type) + payload_bytes

    def _read_packet(self) -> tuple:
        """Read a packet from the socket"""
        # Read length
        length_data = self.socket.recv(4)
        if len(length_data) < 4:
            raise ConnectionError("Connection lost")

        length = struct.unpack("<i", length_data)[0]
        if length < 0 or length > self.MAX_PACKET_SIZE:
            raise ConnectionError(f"RCON packet size out of bounds: {length}")

        # Read rest of packet
        data = self.socket.recv(length)
        request_id = struct.unpack("<i", data[0:4])[0]
        packet_type = struct.unpack("<i", data[4:8])[0]
        payload = data[8:-2].decode("utf-8")

        return request_id, packet_type, payload

    def connect(self) -> bool:
        """Connect and authenticate"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(5.0)
            self.socket.connect((self.host, self.port))

            # Send auth packet
            self.socket.send(self._pack_packet(self.SERVERDATA_AUTH, self.password))

            # Read response
            request_id, packet_type, _ = self._read_packet()

            # Auth success if request_id matches (failure returns -1)
            if request_id != -1:
                return True

            self.disconnect()
            return False

        except Exception as e:
            logger.warning("[RCON] Connection failed: %s", e)
            self.disconnect()
            return False

    def send_command(self, command: str) -> str:
        """Send a command and get response"""
        if not self.socket:
            raise ConnectionError("Not connected")

        try:
            self.socket.send(self._pack_packet(self.SERVERDATA_EXECCOMMAND, command))
            _, _, payload = self._read_packet()
            return payload
        except Exception as e:
            raise ConnectionError(f"Command failed: {e}")

    def disconnect(self):
        """Close connection"""
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None
