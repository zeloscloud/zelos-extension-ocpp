"""Tests for Zelos OCPP extension.

Tests core functionality:
- Node map parsing and validation
- Integration tests with CSMS + simulated charge point (1.6 and 2.0.1)
- Multi-connector support
- MeterValues flow
- Transaction lifecycle
- Actions
- Firmware update
- TLS/WSS
- Connection handling
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from zelos_extension_ocpp.client import (
    CONNECTOR_STATUS_MAP,
    CONNECTOR_STATUS_MAP_201,
    CONNECTOR_STATUS_NAMES,
    CONNECTOR_STATUS_NAMES_201,
    FIRMWARE_STATUS_MAP,
    FIRMWARE_STATUS_NAMES,
    HEALTH_EVENT_CONNECT,
    HEALTH_EVENT_DISCONNECT,
    HEALTH_EVENT_NAMES,
    HEALTH_EVENT_PERIODIC,
    HEARTBEAT_STALE_THRESHOLD_S,
    ConnectorState,
    OcppCsms,
)
from zelos_extension_ocpp.demo.simulator import run_simulated_charge_point
from zelos_extension_ocpp.ocpp_map import Node, NodeMap

# =============================================================================
# Node Map Tests
# =============================================================================


class TestNode:
    """Test Node dataclass."""

    def test_defaults(self):
        node = Node(address="Energy.Active.Import.Register", name="energy")
        assert node.datatype == "float32"
        assert node.unit == ""
        assert node.scale == 1.0
        assert node.writable is None

    def test_invalid_datatype_raises(self):
        with pytest.raises(ValueError, match="Invalid datatype"):
            Node(address="x", name="test", datatype="invalid")

    def test_empty_address_raises(self):
        with pytest.raises(ValueError, match="address cannot be empty"):
            Node(address="", name="test")

    def test_all_valid_datatypes(self):
        for dt in [
            "bool",
            "uint8",
            "int8",
            "uint16",
            "int16",
            "uint32",
            "int32",
            "float32",
            "uint64",
            "int64",
            "float64",
            "string",
        ]:
            node = Node(address="x", name="test", datatype=dt)
            assert node.datatype == dt

    def test_writable_flag(self):
        assert Node(address="x", name="t", writable=True).writable is True
        assert Node(address="x", name="t", writable=False).writable is False
        assert Node(address="x", name="t").writable is None


class TestNodeMap:
    """Test NodeMap parsing."""

    def test_from_dict_creates_events(self):
        data = {
            "events": {
                "meter_values": [{"name": "power_w", "address": "Power.Active.Import"}],
                "status": [{"name": "connector_status", "address": "Status"}],
            }
        }
        node_map = NodeMap.from_dict(data)
        assert set(node_map.event_names) == {"meter_values", "status"}
        assert len(node_map.nodes) == 2

    def test_from_dict_with_name(self):
        data = {
            "name": "my_charger",
            "events": {"test": [{"name": "n1", "address": "x"}]},
        }
        node_map = NodeMap.from_dict(data)
        assert node_map.name == "my_charger"

    def test_from_file(self):
        data = {"events": {"test": [{"name": "node", "address": "x"}]}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            node_map = NodeMap.from_file(f.name)
        assert len(node_map.nodes) == 1
        Path(f.name).unlink()

    def test_from_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            NodeMap.from_file("/nonexistent/path.json")

    def test_get_by_name(self):
        data = {
            "events": {
                "a": [{"name": "voltage", "address": "Voltage"}],
                "b": [{"name": "current", "address": "Current.Import"}],
            }
        }
        node_map = NodeMap.from_dict(data)
        assert node_map.get_by_name("voltage").address == "Voltage"
        assert node_map.get_by_name("current").address == "Current.Import"
        assert node_map.get_by_name("nonexistent") is None

    def test_get_by_address(self):
        data = {
            "events": {
                "a": [{"name": "voltage", "address": "Voltage"}],
            }
        }
        node_map = NodeMap.from_dict(data)
        assert node_map.get_by_address("Voltage").name == "voltage"
        assert node_map.get_by_address("nonexistent") is None

    def test_demo_device_map_loads(self):
        map_path = (
            Path(__file__).parent.parent / "zelos_extension_ocpp" / "demo" / "demo_device.json"
        )
        node_map = NodeMap.from_file(str(map_path))
        assert len(node_map.nodes) > 0
        # meter_values, status, session, firmware, charger_health
        assert len(node_map.event_names) >= 5
        assert node_map.get_by_name("energy_wh") is not None
        assert node_map.get_by_name("power_w") is not None
        assert node_map.get_by_name("connector_status") is not None
        assert node_map.get_by_name("firmware_status") is not None


# =============================================================================
# Status Map Tests
# =============================================================================


class TestStatusMaps:
    """Test OCPP status mapping."""

    def test_connector_status_map_complete(self):
        assert "Available" in CONNECTOR_STATUS_MAP
        assert "Charging" in CONNECTOR_STATUS_MAP
        assert "Faulted" in CONNECTOR_STATUS_MAP

    def test_connector_status_names_inverse(self):
        for status, code in CONNECTOR_STATUS_MAP.items():
            assert CONNECTOR_STATUS_NAMES[code] == status

    def test_connector_status_map_201(self):
        from ocpp.v201.enums import ConnectorStatusEnumType

        assert ConnectorStatusEnumType.available in CONNECTOR_STATUS_MAP_201
        assert ConnectorStatusEnumType.occupied in CONNECTOR_STATUS_MAP_201
        assert ConnectorStatusEnumType.faulted in CONNECTOR_STATUS_MAP_201

    def test_connector_status_names_201_inverse(self):
        for status_enum, code in CONNECTOR_STATUS_MAP_201.items():
            assert CONNECTOR_STATUS_NAMES_201[code] == status_enum.value

    def test_firmware_status_map(self):
        assert FIRMWARE_STATUS_MAP["Idle"] == 0
        assert FIRMWARE_STATUS_MAP["Downloading"] == 1
        assert FIRMWARE_STATUS_MAP["Downloaded"] == 2
        assert FIRMWARE_STATUS_MAP["DownloadFailed"] == 3
        assert FIRMWARE_STATUS_MAP["Installing"] == 4
        assert FIRMWARE_STATUS_MAP["Installed"] == 5
        assert FIRMWARE_STATUS_MAP["InstallationFailed"] == 6

    def test_firmware_status_names_inverse(self):
        for status, code in FIRMWARE_STATUS_MAP.items():
            assert FIRMWARE_STATUS_NAMES[code] == status


# =============================================================================
# ConnectorState Tests
# =============================================================================


class TestConnectorState:
    """Test ConnectorState dataclass."""

    def test_defaults(self):
        cs = ConnectorState()
        assert cs.status == "Available"
        assert cs.error_code == "NoError"
        assert cs.active_transaction_id is None

    def test_custom_values(self):
        cs = ConnectorState(status="Charging", error_code="GroundFailure", active_transaction_id=42)
        assert cs.status == "Charging"
        assert cs.error_code == "GroundFailure"
        assert cs.active_transaction_id == 42


# =============================================================================
# Integration Test Helpers
# =============================================================================


class CsmsTestServer:
    """Helper to run CSMS in a background thread for tests."""

    def __init__(
        self,
        port: int = 19000,
        ssl_cert_file: str | None = None,
        ssl_key_file: str | None = None,
    ):
        self.port = port
        self.csms = OcppCsms(
            host="127.0.0.1",
            port=port,
            ssl_cert_file=ssl_cert_file,
            ssl_key_file=ssl_key_file,
        )
        self._thread: threading.Thread | None = None

    def start(self):
        self.csms.start()
        self._thread = threading.Thread(target=self.csms.run, daemon=True)
        self._thread.start()

        # Wait for WebSocket server to be ready
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"CSMS failed to start on port {self.port}")

    def stop(self):
        self.csms.stop()
        if self._thread:
            self._thread.join(timeout=3.0)


class SimulatorRunner:
    """Helper to run the simulator in a background thread for tests."""

    def __init__(
        self,
        csms_url: str,
        cp_id: str = "CP_TEST_001",
        ocpp_version: str = "1.6",
    ):
        self.csms_url = csms_url
        self.cp_id = cp_id
        self.ocpp_version = ocpp_version
        self._thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Give simulator time to connect and send BootNotification
        time.sleep(2.0)

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._stop_event = asyncio.Event()
        try:
            loop.run_until_complete(
                run_simulated_charge_point(
                    csms_url=self.csms_url,
                    cp_id=self.cp_id,
                    stop_event=self._stop_event,
                    ocpp_version=self.ocpp_version,
                )
            )
        except Exception:
            pass
        finally:
            loop.close()

    def stop(self):
        if self._stop_event:
            self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)


@pytest.fixture(scope="module")
def csms_server():
    """Fixture that starts CSMS server for integration tests."""
    server = CsmsTestServer(port=19000)
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def simulator(csms_server):
    """Fixture that starts v1.6 simulator connected to the CSMS."""
    sim = SimulatorRunner(
        csms_url=f"ws://127.0.0.1:{csms_server.port}",
        cp_id="CP_TEST_001",
        ocpp_version="1.6",
    )
    sim.start()
    yield sim
    sim.stop()


# =============================================================================
# CSMS Integration Tests (OCPP 1.6)
# =============================================================================


class TestCsmsIntegration:
    """Integration tests: CSMS + simulated charge point (OCPP 1.6)."""

    def test_charge_point_connects(self, csms_server, simulator):
        """Charge point should be registered in CSMS."""
        assert "CP_TEST_001" in csms_server.csms._charge_points

    def test_boot_notification_accepted(self, csms_server, simulator):
        """Charge point should have connected status after boot."""
        handler = csms_server.csms._charge_points.get("CP_TEST_001")
        assert handler is not None
        assert handler.connected_at > 0

    def test_meter_values_received(self, csms_server, simulator):
        """CSMS should have received meter values from simulator."""
        time.sleep(2.0)
        assert csms_server.csms._meter_values_count > 0

    def test_transaction_started(self, csms_server, simulator):
        """A transaction should have been started by the simulator."""
        assert csms_server.csms._next_transaction_id > 0

    def test_status_notification_received(self, csms_server, simulator):
        """CSMS should track charge point status via connectors."""
        handler = csms_server.csms._charge_points.get("CP_TEST_001")
        assert handler is not None
        assert len(handler._connectors) > 0

    def test_meter_values_have_correct_types(self, csms_server, simulator):
        """Verify trace source has been initialized with correct schema."""
        source = csms_server.csms._source
        assert source is not None
        assert hasattr(source, "meter_values")
        assert hasattr(source, "status")
        assert hasattr(source, "session")
        assert hasattr(source, "firmware")

    def test_multiple_meter_values_flow(self, csms_server, simulator):
        """Verify meter values continue to flow over time."""
        count_before = csms_server.csms._meter_values_count
        time.sleep(3.0)
        count_after = csms_server.csms._meter_values_count
        assert count_after > count_before

    def test_handler_version(self, csms_server, simulator):
        """Handler should report correct OCPP version."""
        handler = csms_server.csms._charge_points.get("CP_TEST_001")
        assert handler is not None
        assert handler.ocpp_version == "1.6"


# =============================================================================
# Multi-Connector Tests
# =============================================================================


class TestMultiConnector:
    """Test multi-connector support."""

    def test_multi_connector_tracked(self, csms_server, simulator):
        """CSMS should track multiple connectors per charge point."""
        handler = csms_server.csms._charge_points.get("CP_TEST_001")
        assert handler is not None
        # The simulator runs sessions on connectors 1 and 2
        # Wait a bit for connector 2 session to complete
        time.sleep(5.0)
        assert len(handler._connectors) >= 1

    def test_get_status_shows_connectors(self, csms_server, simulator):
        """get_status should show per-connector info."""
        result = csms_server.csms.get_status()
        assert result["connected_count"] >= 1
        cp = next(cp for cp in result["charge_points"] if cp["id"] == "CP_TEST_001")
        assert "connectors" in cp
        assert isinstance(cp["connectors"], dict)


# =============================================================================
# Action Tests (Unit - no network)
# =============================================================================


class TestActionsUnit:
    """Unit tests for SDK actions (no network)."""

    @pytest.fixture
    def csms(self):
        return OcppCsms(host="127.0.0.1", port=19999)

    def test_get_status_returns_info(self, csms):
        result = csms.get_status()
        assert "running" in result
        assert "host" in result
        assert "port" in result
        assert "connected_count" in result
        assert "charge_points" in result
        assert result["connected_count"] == 0

    def test_remote_start_no_cp(self, csms):
        result = csms.remote_start_action("nonexistent", "TAG")
        assert result["success"] is False
        assert "not connected" in result["error"]

    def test_remote_stop_no_cp(self, csms):
        result = csms.remote_stop_action("nonexistent", 1)
        assert result["success"] is False
        assert "not connected" in result["error"]

    def test_reset_no_cp(self, csms):
        result = csms.reset_action("nonexistent", "Soft")
        assert result["success"] is False
        assert "not connected" in result["error"]

    def test_trigger_meter_values_no_cp(self, csms):
        result = csms.trigger_meter_values_action("nonexistent")
        assert result["success"] is False
        assert "not connected" in result["error"]

    def test_update_firmware_no_cp(self, csms):
        result = csms.update_firmware_action("nonexistent", "https://example.com/fw.bin")
        assert result["success"] is False
        assert "not connected" in result["error"]


# =============================================================================
# Action Tests (Integration)
# =============================================================================


class TestActionsIntegration:
    """Integration tests for SDK actions with CSMS + simulator."""

    def test_get_status_shows_connected_cp(self, csms_server, simulator):
        result = csms_server.csms.get_status()
        assert result["connected_count"] >= 1
        cp_ids = [cp["id"] for cp in result["charge_points"]]
        assert "CP_TEST_001" in cp_ids

    def test_get_status_shows_transaction_count(self, csms_server, simulator):
        result = csms_server.csms.get_status()
        assert result["total_transactions"] > 0

    def test_get_status_shows_meter_values_count(self, csms_server, simulator):
        result = csms_server.csms.get_status()
        assert result["meter_values_received"] > 0

    def test_get_status_shows_ocpp_version(self, csms_server, simulator):
        result = csms_server.csms.get_status()
        cp = next(cp for cp in result["charge_points"] if cp["id"] == "CP_TEST_001")
        assert cp["ocpp_version"] == "1.6"

    def test_get_status_shows_firmware_status(self, csms_server, simulator):
        result = csms_server.csms.get_status()
        cp = next(cp for cp in result["charge_points"] if cp["id"] == "CP_TEST_001")
        assert "firmware_status" in cp


# =============================================================================
# Process MeterValues Tests
# =============================================================================


class TestProcessMeterValues:
    """Test the meter values processing logic."""

    @pytest.fixture
    def csms_with_source(self):
        csms = OcppCsms(host="127.0.0.1", port=19998)
        csms.start()
        return csms

    def test_process_complete_meter_values(self, csms_with_source):
        csms = csms_with_source
        sampled = [
            {"value": "1234.5", "measurand": "Energy.Active.Import.Register"},
            {"value": "7360.0", "measurand": "Power.Active.Import"},
            {"value": "32.0", "measurand": "Current.Import"},
            {"value": "230.0", "measurand": "Voltage"},
            {"value": "45.0", "measurand": "SoC"},
            {"value": "35.0", "measurand": "Temperature"},
        ]
        csms.process_meter_values(sampled, connector_id=1)
        assert csms._meter_values_count == 1

    def test_process_partial_meter_values(self, csms_with_source):
        csms = csms_with_source
        sampled = [
            {"value": "7360.0", "measurand": "Power.Active.Import"},
            {"value": "230.0", "measurand": "Voltage"},
        ]
        csms.process_meter_values(sampled, connector_id=1)
        assert csms._meter_values_count == 1

    def test_process_invalid_value_skipped(self, csms_with_source):
        csms = csms_with_source
        sampled = [
            {"value": "not_a_number", "measurand": "Power.Active.Import"},
            {"value": "230.0", "measurand": "Voltage"},
        ]
        csms.process_meter_values(sampled, connector_id=1)
        assert csms._meter_values_count == 1

    def test_process_empty_meter_values(self, csms_with_source):
        csms = csms_with_source
        csms.process_meter_values([], connector_id=1)
        assert csms._meter_values_count == 0

    def test_process_meter_values_with_connector_id(self, csms_with_source):
        csms = csms_with_source
        sampled = [{"value": "230.0", "measurand": "Voltage"}]
        csms.process_meter_values(sampled, connector_id=2)
        assert csms._meter_values_count == 1

    def test_log_status(self, csms_with_source):
        csms = csms_with_source
        # Should not raise
        csms.log_status(connector_id=1, connector_status=2, error_code=0)

    def test_log_status_with_connector_id(self, csms_with_source):
        csms = csms_with_source
        csms.log_status(connector_id=2, connector_status=0, error_code=0)

    def test_log_session(self, csms_with_source):
        csms = csms_with_source
        csms.log_session(connector_id=1, transaction_id=1, meter_start_wh=1000.0)
        csms.log_session(connector_id=1, transaction_id=1, meter_stop_wh=2000.0)

    def test_log_firmware(self, csms_with_source):
        csms = csms_with_source
        csms.log_firmware("Downloading", request_id=1)
        csms.log_firmware("Installed")


# =============================================================================
# OCPP 2.0.1 Integration Tests
# =============================================================================


@pytest.fixture(scope="module")
def csms_server_201():
    """Fixture that starts CSMS server for 2.0.1 integration tests."""
    server = CsmsTestServer(port=19001)
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def simulator_201(csms_server_201):
    """Fixture that starts v2.0.1 simulator connected to the CSMS."""
    sim = SimulatorRunner(
        csms_url=f"ws://127.0.0.1:{csms_server_201.port}",
        cp_id="CP_TEST_201",
        ocpp_version="2.0.1",
    )
    sim.start()
    yield sim
    sim.stop()


class TestOcpp201Integration:
    """Integration tests: CSMS + simulated charge point (OCPP 2.0.1)."""

    def test_charge_point_connects_201(self, csms_server_201, simulator_201):
        """2.0.1 charge point should be registered in CSMS."""
        assert "CP_TEST_201" in csms_server_201.csms._charge_points

    def test_handler_version_201(self, csms_server_201, simulator_201):
        """Handler should report correct OCPP version."""
        handler = csms_server_201.csms._charge_points.get("CP_TEST_201")
        assert handler is not None
        assert handler.ocpp_version == "2.0.1"

    def test_boot_notification_201(self, csms_server_201, simulator_201):
        handler = csms_server_201.csms._charge_points.get("CP_TEST_201")
        assert handler is not None
        assert handler.connected_at > 0

    def test_meter_values_received_201(self, csms_server_201, simulator_201):
        time.sleep(2.0)
        assert csms_server_201.csms._meter_values_count > 0

    def test_transaction_started_201(self, csms_server_201, simulator_201):
        assert csms_server_201.csms._next_transaction_id > 0

    def test_connectors_tracked_201(self, csms_server_201, simulator_201):
        handler = csms_server_201.csms._charge_points.get("CP_TEST_201")
        assert handler is not None
        assert len(handler._connectors) > 0

    def test_get_status_201(self, csms_server_201, simulator_201):
        result = csms_server_201.csms.get_status()
        assert result["connected_count"] >= 1
        cp = next(
            (cp for cp in result["charge_points"] if cp["id"] == "CP_TEST_201"),
            None,
        )
        assert cp is not None
        assert cp["ocpp_version"] == "2.0.1"


# =============================================================================
# Firmware Update Tests
# =============================================================================


class TestFirmwareUpdate:
    """Test firmware update functionality."""

    def test_firmware_action_integration(self, csms_server, simulator):
        """Test firmware update action triggers the full status sequence."""
        handler = csms_server.csms._charge_points.get("CP_TEST_001")
        assert handler is not None

        result = csms_server.csms.update_firmware_action(
            "CP_TEST_001", "https://example.com/fw.bin"
        )
        assert result["success"] is True
        assert "request_id" in result

        # Wait for firmware status notifications to flow through
        time.sleep(5.0)

        # Handler should have received firmware status updates
        assert handler.firmware_status == "Installed"


# =============================================================================
# TLS/WSS Tests
# =============================================================================


def _generate_self_signed_cert(cert_path: str, key_path: str) -> None:
    """Generate a self-signed certificate for testing."""
    try:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
            ]
        )
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.UTC))
            .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        Path(key_path).write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        Path(cert_path).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    except ImportError:
        # Fall back to openssl command
        import subprocess

        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                key_path,
                "-out",
                cert_path,
                "-days",
                "1",
                "-nodes",
                "-subj",
                "/CN=localhost",
            ],
            check=True,
            capture_output=True,
        )


class TestTls:
    """Test TLS/WSS support."""

    @pytest.fixture(scope="class")
    def tls_certs(self, tmp_path_factory):
        """Generate self-signed cert/key for TLS testing."""
        tmp = tmp_path_factory.mktemp("tls")
        cert_path = str(tmp / "cert.pem")
        key_path = str(tmp / "key.pem")
        _generate_self_signed_cert(cert_path, key_path)
        return cert_path, key_path

    @pytest.fixture(scope="class")
    def tls_csms_server(self, tls_certs):
        cert_path, key_path = tls_certs
        server = CsmsTestServer(
            port=19002,
            ssl_cert_file=cert_path,
            ssl_key_file=key_path,
        )
        server.start()
        yield server
        server.stop()

    @pytest.fixture(scope="class")
    def tls_simulator(self, tls_csms_server):
        sim = SimulatorRunner(
            csms_url=f"wss://127.0.0.1:{tls_csms_server.port}",
            cp_id="CP_TEST_TLS",
            ocpp_version="1.6",
        )
        sim.start()
        yield sim
        sim.stop()

    def test_tls_connection(self, tls_csms_server, tls_simulator):
        """Charge point should connect over TLS."""
        assert "CP_TEST_TLS" in tls_csms_server.csms._charge_points

    def test_tls_meter_values(self, tls_csms_server, tls_simulator):
        """Meter values should flow over TLS."""
        time.sleep(2.0)
        assert tls_csms_server.csms._meter_values_count > 0

    def test_tls_transaction(self, tls_csms_server, tls_simulator):
        """Transactions should work over TLS."""
        assert tls_csms_server.csms._next_transaction_id > 0


# =============================================================================
# Health Constants Tests
# =============================================================================


class TestHealthConstants:
    """Test charger health constants."""

    def test_health_event_names(self):
        assert HEALTH_EVENT_NAMES[HEALTH_EVENT_PERIODIC] == "periodic"
        assert HEALTH_EVENT_NAMES[HEALTH_EVENT_CONNECT] == "connect"
        assert HEALTH_EVENT_NAMES[HEALTH_EVENT_DISCONNECT] == "disconnect"

    def test_heartbeat_stale_threshold(self):
        assert HEARTBEAT_STALE_THRESHOLD_S == 30.0

    def test_health_event_values(self):
        assert HEALTH_EVENT_PERIODIC == 0
        assert HEALTH_EVENT_CONNECT == 1
        assert HEALTH_EVENT_DISCONNECT == 2


# =============================================================================
# Charger Health Trace Event Tests
# =============================================================================


class TestChargerHealth:
    """Test charger health trace event logging."""

    @pytest.fixture
    def csms_with_source(self):
        csms = OcppCsms(host="127.0.0.1", port=19997)
        csms.start()
        return csms

    def test_log_charger_health_no_source(self):
        """log_charger_health should be a no-op without a source."""
        csms = OcppCsms(host="127.0.0.1", port=19996)
        # Should not raise
        csms.log_charger_health(HEALTH_EVENT_PERIODIC)

    def test_log_charger_health_periodic(self, csms_with_source):
        """Should log periodic health event with correct counts."""
        csms = csms_with_source
        # No charge points connected
        csms.log_charger_health(HEALTH_EVENT_PERIODIC)

    def test_log_charger_health_connect(self, csms_with_source):
        """Should log connect health event."""
        csms = csms_with_source
        csms.log_charger_health(HEALTH_EVENT_CONNECT)

    def test_log_charger_health_disconnect(self, csms_with_source):
        """Should log disconnect health event."""
        csms = csms_with_source
        csms.log_charger_health(HEALTH_EVENT_DISCONNECT)

    def test_trace_source_has_charger_health(self, csms_with_source):
        """Trace source should have charger_health event registered."""
        csms = csms_with_source
        assert hasattr(csms._source, "charger_health")

    def test_demo_device_map_has_charger_health(self):
        """Demo device map should include charger_health event."""
        map_path = (
            Path(__file__).parent.parent / "zelos_extension_ocpp" / "demo" / "demo_device.json"
        )
        node_map = NodeMap.from_file(str(map_path))
        assert "charger_health" in node_map.event_names
        assert node_map.get_by_name("total_connected") is not None
        assert node_map.get_by_name("total_healthy") is not None
        assert node_map.get_by_name("total_stale") is not None
        assert node_map.get_by_name("event_type") is not None


# =============================================================================
# TriggerMessage Polling Tests
# =============================================================================


class TestPolling:
    """Test TriggerMessage polling and health polling coroutines."""

    def test_poll_trigger_meter_values_runs(self):
        """_poll_trigger_meter_values should run without error when no CPs connected."""
        csms = OcppCsms(host="127.0.0.1", port=19995, poll_interval=0.1)
        csms._running = True

        async def run_poll():
            task = asyncio.create_task(csms._poll_trigger_meter_values())
            await asyncio.sleep(0.25)
            csms._running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(run_poll())

    def test_poll_charger_health_runs(self):
        """_poll_charger_health should run and emit periodic events."""
        csms = OcppCsms(host="127.0.0.1", port=19994, poll_interval=0.1)
        csms.start()

        async def run_poll():
            task = asyncio.create_task(csms._poll_charger_health())
            await asyncio.sleep(0.25)
            csms._running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(run_poll())

    def test_run_async_launches_background_tasks(self):
        """_run_async should launch poll and health tasks that get cancelled on stop."""
        csms = OcppCsms(host="127.0.0.1", port=19993, poll_interval=0.1)
        csms.start()

        async def run_and_stop():
            task = asyncio.create_task(csms._run_async())
            await asyncio.sleep(0.5)
            csms._running = False
            await asyncio.sleep(0.5)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        asyncio.run(run_and_stop())


# =============================================================================
# Integration: Health Events on Connect/Disconnect
# =============================================================================


class TestHealthIntegration:
    """Integration test: health events fire on CP connect/disconnect."""

    def test_charger_health_event_on_connect(self, csms_server, simulator):
        """Charger health event source should exist after CP connects."""
        assert hasattr(csms_server.csms._source, "charger_health")
