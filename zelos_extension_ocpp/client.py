"""OCPP 1.6 + 2.0.1 Central System (CSMS) with Zelos SDK integration.

Runs a WebSocket server that charge points connect to. Handles OCPP 1.6
and 2.0.1 messages and converts MeterValues to Zelos trace events.
Supports multi-connector charge points and TLS/WSS.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from dataclasses import dataclass
from typing import Any

import zelos_sdk
from ocpp.routing import on
from ocpp.v16 import ChargePoint as Cp16
from ocpp.v16 import call as call16
from ocpp.v16 import call_result as call_result16
from ocpp.v16.enums import Action as Action16
from ocpp.v16.enums import (
    AuthorizationStatus,
    ChargePointStatus,
    RegistrationStatus,
)
from ocpp.v201 import ChargePoint as Cp201
from ocpp.v201 import call as call201
from ocpp.v201 import call_result as call_result201
from ocpp.v201.enums import Action as Action201
from ocpp.v201.enums import AuthorizationStatusEnumType as AuthStatus201
from ocpp.v201.enums import ConnectorStatusEnumType as ConnStatus201
from ocpp.v201.enums import (
    IdTokenEnumType,
    MessageTriggerEnumType,
    ResetEnumType,
    TransactionEventEnumType,
)
from ocpp.v201.enums import RegistrationStatusEnumType as RegStatus201

try:
    from websockets.asyncio.server import ServerConnection, serve
except ImportError:
    from websockets import serve

    ServerConnection = Any  # type: ignore[assignment,misc]

from zelos_extension_ocpp.charger_id import ChargerPathResolver
from zelos_extension_ocpp.ocpp_map import NodeMap

logger = logging.getLogger(__name__)

# Map from node map datatype strings to Zelos SDK DataType
SDK_DATATYPE_MAP = {
    "bool": zelos_sdk.DataType.Boolean,
    "uint8": zelos_sdk.DataType.UInt8,
    "int8": zelos_sdk.DataType.Int8,
    "uint16": zelos_sdk.DataType.UInt16,
    "int16": zelos_sdk.DataType.Int16,
    "uint32": zelos_sdk.DataType.UInt32,
    "int32": zelos_sdk.DataType.Int32,
    "float32": zelos_sdk.DataType.Float32,
    "uint64": zelos_sdk.DataType.UInt64,
    "int64": zelos_sdk.DataType.Int64,
    "float64": zelos_sdk.DataType.Float64,
}

# OCPP 1.6 connector status to uint8 mapping
CONNECTOR_STATUS_MAP = {
    ChargePointStatus.available: 0,
    ChargePointStatus.preparing: 1,
    ChargePointStatus.charging: 2,
    ChargePointStatus.suspended_evse: 3,
    ChargePointStatus.suspended_ev: 4,
    ChargePointStatus.finishing: 5,
    ChargePointStatus.reserved: 6,
    ChargePointStatus.unavailable: 7,
    ChargePointStatus.faulted: 8,
}

CONNECTOR_STATUS_NAMES = {v: k for k, v in CONNECTOR_STATUS_MAP.items()}

# OCPP 2.0.1 connector status to uint8 mapping
CONNECTOR_STATUS_MAP_201 = {
    ConnStatus201.available: 0,
    ConnStatus201.occupied: 1,
    ConnStatus201.reserved: 2,
    ConnStatus201.unavailable: 3,
    ConnStatus201.faulted: 4,
}

CONNECTOR_STATUS_NAMES_201 = {v: k.value for k, v in CONNECTOR_STATUS_MAP_201.items()}

# Firmware status to uint8 mapping (shared across versions)
FIRMWARE_STATUS_MAP = {
    "Idle": 0,
    "Downloading": 1,
    "Downloaded": 2,
    "DownloadFailed": 3,
    "Installing": 4,
    "Installed": 5,
    "InstallationFailed": 6,
}

FIRMWARE_STATUS_NAMES = {v: k for k, v in FIRMWARE_STATUS_MAP.items()}

# Charger health event constants
HEARTBEAT_STALE_THRESHOLD_S = 30.0
HEALTH_EVENT_PERIODIC = 0
HEALTH_EVENT_CONNECT = 1
HEALTH_EVENT_DISCONNECT = 2
HEALTH_EVENT_NAMES = {0: "periodic", 1: "connect", 2: "disconnect"}


@dataclass
class ConnectorState:
    """Per-connector state tracked by the CSMS."""

    status: str = "Available"
    error_code: str = "NoError"
    active_transaction_id: int | None = None


class ChargePointHandler16(Cp16):
    """OCPP 1.6 charge point handler for the CSMS side."""

    ocpp_version: str = "1.6"

    def __init__(self, id: str, connection, csms: OcppCsms):
        super().__init__(id, connection)
        self.csms = csms
        self.connected_at = time.time()
        self.last_heartbeat = time.time()
        self._connectors: dict[int, ConnectorState] = {}
        self.firmware_status: str = "Idle"
        self.firmware_request_id: int | None = None

    def _get_connector(self, connector_id: int) -> ConnectorState:
        """Get or create connector state."""
        if connector_id not in self._connectors:
            self._connectors[connector_id] = ConnectorState()
        return self._connectors[connector_id]

    @on(Action16.boot_notification)
    def on_boot_notification(self, charge_point_vendor: str, charge_point_model: str, **kwargs):
        logger.info(
            f"[CSMS] BootNotification from {self.id}: "
            f"vendor={charge_point_vendor}, model={charge_point_model}"
        )
        # OCPP 1.6 BootNotification carries a richer set of fields than the two
        # required positional ones — surface them all in the info trace.
        self.csms.log_info(
            self.id,
            vendor=charge_point_vendor,
            model=charge_point_model,
            serial_number=str(
                kwargs.get("charge_point_serial_number")
                or kwargs.get("charge_box_serial_number")
                or ""
            ),
            firmware_version=str(kwargs.get("firmware_version") or ""),
            ocpp_version=self.ocpp_version,
        )
        return call_result16.BootNotification(
            current_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            interval=10,
            status=RegistrationStatus.accepted,
        )

    @on(Action16.heartbeat)
    def on_heartbeat(self, **kwargs):
        self.last_heartbeat = time.time()
        return call_result16.Heartbeat(
            current_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    @on(Action16.status_notification)
    def on_status_notification(self, connector_id: int, error_code: str, status: str, **kwargs):
        logger.info(
            f"[CSMS] StatusNotification from {self.id}: "
            f"connector={connector_id}, status={status}, error={error_code}"
        )
        conn = self._get_connector(connector_id)
        conn.status = status
        conn.error_code = error_code

        status_int = CONNECTOR_STATUS_MAP.get(status, 0)
        error_int = 0 if error_code == "NoError" else 1
        self.csms.log_status(self.id, connector_id, status_int, error_int)

        return call_result16.StatusNotification()

    @on(Action16.meter_values)
    def on_meter_values(self, connector_id: int, meter_value: list, **kwargs):
        logger.debug(f"[CSMS] MeterValues from {self.id}: connector={connector_id}")

        for mv in meter_value:
            sampled_values = mv.get("sampled_value", [])
            self.csms.process_meter_values(self.id, sampled_values, connector_id=connector_id)

        return call_result16.MeterValues()

    @on(Action16.authorize)
    def on_authorize(self, id_tag: str, **kwargs):
        logger.info(f"[CSMS] Authorize request: id_tag={id_tag}")
        return call_result16.Authorize(
            id_tag_info={"status": AuthorizationStatus.accepted},
        )

    @on(Action16.start_transaction)
    def on_start_transaction(
        self, connector_id: int, id_tag: str, meter_start: int, timestamp: str, **kwargs
    ):
        self.csms._next_transaction_id += 1
        txn_id = self.csms._next_transaction_id
        conn = self._get_connector(connector_id)
        conn.active_transaction_id = txn_id

        logger.info(
            f"[CSMS] StartTransaction from {self.id}: "
            f"connector={connector_id}, id_tag={id_tag}, txn_id={txn_id}"
        )

        self.csms.log_session(self.id, connector_id, txn_id, meter_start_wh=float(meter_start))

        return call_result16.StartTransaction(
            transaction_id=txn_id,
            id_tag_info={"status": AuthorizationStatus.accepted},
        )

    @on(Action16.stop_transaction)
    def on_stop_transaction(self, meter_stop: int, timestamp: str, transaction_id: int, **kwargs):
        logger.info(
            f"[CSMS] StopTransaction from {self.id}: "
            f"txn_id={transaction_id}, meter_stop={meter_stop}"
        )

        # Find which connector has this transaction
        connector_id = 1
        for cid, conn in self._connectors.items():
            if conn.active_transaction_id == transaction_id:
                conn.active_transaction_id = None
                connector_id = cid
                break

        self.csms.log_session(
            self.id, connector_id, transaction_id, meter_stop_wh=float(meter_stop)
        )

        return call_result16.StopTransaction(
            id_tag_info={"status": AuthorizationStatus.accepted},
        )

    @on(Action16.firmware_status_notification)
    def on_firmware_status_notification(self, status: str, **kwargs):
        logger.info(f"[CSMS] FirmwareStatusNotification from {self.id}: status={status}")
        self.firmware_status = status
        self.csms.log_firmware(self.id, status)
        return call_result16.FirmwareStatusNotification()

    # -- Version-specific action dispatch methods --

    async def remote_start(self, id_tag: str):
        return await self.call(call16.RemoteStartTransaction(id_tag=id_tag))

    async def remote_stop(self, transaction_id: int):
        return await self.call(call16.RemoteStopTransaction(transaction_id=transaction_id))

    async def reset(self, reset_type: str):
        return await self.call(call16.Reset(type=reset_type))

    async def trigger_meter_values(self):
        return await self.call(call16.TriggerMessage(requested_message="MeterValues"))

    async def update_firmware(self, location: str, request_id: int):
        self.firmware_request_id = request_id
        return await self.call(
            call16.UpdateFirmware(
                location=location,
                retrieve_date=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        )


class ChargePointHandler201(Cp201):
    """OCPP 2.0.1 charge point handler for the CSMS side."""

    ocpp_version: str = "2.0.1"

    def __init__(self, id: str, connection, csms: OcppCsms):
        super().__init__(id, connection)
        self.csms = csms
        self.connected_at = time.time()
        self.last_heartbeat = time.time()
        self._connectors: dict[int, ConnectorState] = {}
        self.firmware_status: str = "Idle"
        self.firmware_request_id: int | None = None
        self._transaction_map: dict[str, int] = {}  # ocpp txn_id (str) -> our int txn_id

    def _get_connector(self, connector_id: int) -> ConnectorState:
        if connector_id not in self._connectors:
            self._connectors[connector_id] = ConnectorState()
        return self._connectors[connector_id]

    @on(Action201.boot_notification)
    def on_boot_notification(self, charging_station: dict, reason: str, **kwargs):
        vendor = str(charging_station.get("vendor_name") or "")
        model = str(charging_station.get("model") or "")
        logger.info(
            f"[CSMS] BootNotification (2.0.1) from {self.id}: "
            f"vendor={vendor or '?'}, model={model or '?'}, reason={reason}"
        )
        modem = charging_station.get("modem") or {}
        self.csms.log_info(
            self.id,
            vendor=vendor,
            model=model,
            serial_number=str(charging_station.get("serial_number") or ""),
            firmware_version=str(charging_station.get("firmware_version") or ""),
            ocpp_version=self.ocpp_version,
        )
        # iccid/imsi are useful operational metadata for cellular-attached CPs;
        # carried by the modem sub-object in 2.0.1 — logged at INFO for now since
        # they aren't part of the trace schema (string fields, optional).
        if modem.get("iccid") or modem.get("imsi"):
            logger.info(
                f"[CSMS] {self.id} modem: iccid={modem.get('iccid')!r} imsi={modem.get('imsi')!r}"
            )
        return call_result201.BootNotification(
            current_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            interval=10,
            status=RegStatus201.accepted,
        )

    @on(Action201.heartbeat)
    def on_heartbeat(self, **kwargs):
        self.last_heartbeat = time.time()
        return call_result201.Heartbeat(
            current_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    @on(Action201.status_notification)
    def on_status_notification(
        self, timestamp: str, connector_status: str, evse_id: int, connector_id: int, **kwargs
    ):
        logger.info(
            f"[CSMS] StatusNotification (2.0.1) from {self.id}: "
            f"evse={evse_id}, connector={connector_id}, status={connector_status}"
        )
        conn = self._get_connector(connector_id)
        conn.status = connector_status

        status_int = CONNECTOR_STATUS_MAP_201.get(connector_status, 0)
        self.csms.log_status(self.id, connector_id, status_int, 0)

        return call_result201.StatusNotification()

    @staticmethod
    def _extract_energy_value(meter_value: list) -> float:
        """Extract energy reading from v201 meter_value list."""
        for mv in meter_value or []:
            for sv in mv.get("sampled_value", []):
                measurand = sv.get("measurand", "Energy.Active.Import.Register")
                if measurand == "Energy.Active.Import.Register":
                    try:
                        return float(sv.get("value", 0))
                    except (ValueError, TypeError):
                        continue
        return 0.0

    @on(Action201.transaction_event)
    def on_transaction_event(
        self,
        event_type: str,
        timestamp: str,
        trigger_reason: str,
        seq_no: int,
        transaction_info: dict,
        **kwargs,
    ):
        ocpp_txn_id = transaction_info.get("transaction_id", "")
        meter_value = kwargs.get("meter_value", [])
        evse = kwargs.get("evse")
        connector_id = evse.get("id", 1) if isinstance(evse, dict) else 1

        if event_type == TransactionEventEnumType.started:
            self.csms._next_transaction_id += 1
            int_txn_id = self.csms._next_transaction_id
            self._transaction_map[ocpp_txn_id] = int_txn_id
            conn = self._get_connector(connector_id)
            conn.active_transaction_id = int_txn_id

            meter_start = self._extract_energy_value(meter_value)

            logger.info(
                f"[CSMS] TransactionEvent Started (2.0.1) from {self.id}: "
                f"txn={ocpp_txn_id}, connector={connector_id}"
            )
            self.csms.log_session(self.id, connector_id, int_txn_id, meter_start_wh=meter_start)

        elif event_type == TransactionEventEnumType.updated:
            int_txn_id = self._transaction_map.get(ocpp_txn_id, 0)
            if meter_value:
                for mv in meter_value:
                    sampled = mv.get("sampled_value", [])
                    # Convert v201 sampled values to the dict format process_meter_values expects
                    converted = []
                    for sv in sampled:
                        entry = {"value": str(sv.get("value", "0"))}
                        if "measurand" in sv:
                            entry["measurand"] = sv["measurand"]
                        converted.append(entry)
                    self.csms.process_meter_values(self.id, converted, connector_id=connector_id)

        elif event_type == TransactionEventEnumType.ended:
            int_txn_id = self._transaction_map.pop(ocpp_txn_id, 0)
            conn = self._get_connector(connector_id)
            if conn.active_transaction_id == int_txn_id:
                conn.active_transaction_id = None

            meter_stop = self._extract_energy_value(meter_value)

            logger.info(
                f"[CSMS] TransactionEvent Ended (2.0.1) from {self.id}: "
                f"txn={ocpp_txn_id}, connector={connector_id}"
            )
            self.csms.log_session(self.id, connector_id, int_txn_id, meter_stop_wh=meter_stop)

        return call_result201.TransactionEvent()

    @on(Action201.meter_values)
    def on_meter_values(self, evse_id: int, meter_value: list, **kwargs):
        logger.debug(f"[CSMS] MeterValues (2.0.1) from {self.id}: evse={evse_id}")

        for mv in meter_value:
            sampled = mv.get("sampled_value", [])
            converted = []
            for sv in sampled:
                entry = {"value": str(sv.get("value", "0"))}
                if "measurand" in sv:
                    entry["measurand"] = sv["measurand"]
                converted.append(entry)
            self.csms.process_meter_values(self.id, converted, connector_id=evse_id)

        return call_result201.MeterValues()

    @on(Action201.authorize)
    def on_authorize(self, id_token: dict, **kwargs):
        token_id = id_token.get("id_token", "unknown") if isinstance(id_token, dict) else "unknown"
        logger.info(f"[CSMS] Authorize (2.0.1) request: id_token={token_id}")
        return call_result201.Authorize(
            id_token_info={"status": AuthStatus201.accepted},
        )

    @on(Action201.firmware_status_notification)
    def on_firmware_status_notification(self, status: str, **kwargs):
        request_id = kwargs.get("request_id")
        logger.info(
            f"[CSMS] FirmwareStatusNotification (2.0.1) from {self.id}: "
            f"status={status}, request_id={request_id}"
        )
        self.firmware_status = status
        self.firmware_request_id = request_id
        self.csms.log_firmware(self.id, status, request_id=request_id)
        return call_result201.FirmwareStatusNotification()

    # -- Version-specific action dispatch methods --

    async def remote_start(self, id_tag: str):
        self.csms._next_remote_start_id += 1
        return await self.call(
            call201.RequestStartTransaction(
                id_token={"id_token": id_tag, "type": IdTokenEnumType.central},
                remote_start_id=self.csms._next_remote_start_id,
            )
        )

    async def remote_stop(self, transaction_id: int):
        # Find the OCPP string txn_id for this int id
        ocpp_txn_id = None
        for otid, itid in self._transaction_map.items():
            if itid == transaction_id:
                ocpp_txn_id = otid
                break
        if ocpp_txn_id is None:
            ocpp_txn_id = str(transaction_id)
        return await self.call(call201.RequestStopTransaction(transaction_id=ocpp_txn_id))

    async def reset(self, reset_type: str):
        type_map = {"Soft": ResetEnumType.on_idle, "Hard": ResetEnumType.immediate}
        return await self.call(call201.Reset(type=type_map.get(reset_type, ResetEnumType.on_idle)))

    async def trigger_meter_values(self):
        return await self.call(
            call201.TriggerMessage(requested_message=MessageTriggerEnumType.meter_values)
        )

    async def update_firmware(self, location: str, request_id: int):
        self.firmware_request_id = request_id
        return await self.call(
            call201.UpdateFirmware(
                request_id=request_id,
                firmware={
                    "location": location,
                    "retrieve_date_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
        )


class OcppCsms:
    """OCPP Central System Management Server with Zelos SDK integration.

    Supports OCPP 1.6 and 2.0.1 with automatic version detection,
    multi-connector tracking, firmware updates, and optional TLS.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9000,
        node_map: NodeMap | None = None,
        poll_interval: float = 10.0,
        ssl_cert_file: str | None = None,
        ssl_key_file: str | None = None,
        charger_aliases: dict[str, str] | None = None,
        charger_aliases_file: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.node_map = node_map
        self.poll_interval = poll_interval
        self.ssl_cert_file = ssl_cert_file
        self.ssl_key_file = ssl_key_file

        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._charge_points: dict[str, ChargePointHandler16 | ChargePointHandler201] = {}
        self._next_transaction_id = 0
        self._next_remote_start_id = 0
        self._next_firmware_request_id = 0
        self._meter_values_count = 0
        self._source: zelos_sdk.TraceSourceCacheLast | None = None

        # Charge-point identity resolution. Aliases passed in-process take
        # precedence; otherwise loaded from a JSON file if provided.
        if charger_aliases is not None:
            self._resolver = ChargerPathResolver(charger_aliases)
        else:
            self._resolver = ChargerPathResolver.from_file(charger_aliases_file)

        # Per-charger event registration bookkeeping.
        self._registered_paths: set[str] = set()
        self._event_templates: dict[str, list[zelos_sdk.TraceEventFieldMetadata]] = {}

    # ---- Default per-charger event schema templates ------------------------
    # Each charger gets its own copy registered under `<charger_path>/<event>`.
    # Charge-point ID is *implicit in the path* — operators browse and plot
    # per-charger streams natively in the Zelos viewer. The `connector_id`
    # field disambiguates connectors within a single charger.

    @staticmethod
    def _default_event_templates() -> dict[str, list[zelos_sdk.TraceEventFieldMetadata]]:
        return {
            "meter_values": [
                zelos_sdk.TraceEventFieldMetadata("connector_id", zelos_sdk.DataType.UInt8, ""),
                zelos_sdk.TraceEventFieldMetadata("energy_wh", zelos_sdk.DataType.Float64, "Wh"),
                zelos_sdk.TraceEventFieldMetadata("power_w", zelos_sdk.DataType.Float32, "W"),
                zelos_sdk.TraceEventFieldMetadata("current_a", zelos_sdk.DataType.Float32, "A"),
                zelos_sdk.TraceEventFieldMetadata("voltage_v", zelos_sdk.DataType.Float32, "V"),
                zelos_sdk.TraceEventFieldMetadata("soc_percent", zelos_sdk.DataType.Float32, "%"),
                zelos_sdk.TraceEventFieldMetadata("temperature_c", zelos_sdk.DataType.Float32, "C"),
            ],
            "status": [
                zelos_sdk.TraceEventFieldMetadata("connector_id", zelos_sdk.DataType.UInt8, ""),
                zelos_sdk.TraceEventFieldMetadata("connector_status", zelos_sdk.DataType.UInt8),
                zelos_sdk.TraceEventFieldMetadata("error_code", zelos_sdk.DataType.UInt8),
            ],
            "session": [
                zelos_sdk.TraceEventFieldMetadata("connector_id", zelos_sdk.DataType.UInt8, ""),
                zelos_sdk.TraceEventFieldMetadata("transaction_id", zelos_sdk.DataType.Int32),
                zelos_sdk.TraceEventFieldMetadata(
                    "meter_start_wh", zelos_sdk.DataType.Float64, "Wh"
                ),
                zelos_sdk.TraceEventFieldMetadata(
                    "meter_stop_wh", zelos_sdk.DataType.Float64, "Wh"
                ),
            ],
            "firmware": [
                zelos_sdk.TraceEventFieldMetadata("firmware_status", zelos_sdk.DataType.UInt8, ""),
                zelos_sdk.TraceEventFieldMetadata("request_id", zelos_sdk.DataType.Int32, ""),
            ],
            "info": [
                zelos_sdk.TraceEventFieldMetadata("vendor", zelos_sdk.DataType.String),
                zelos_sdk.TraceEventFieldMetadata("model", zelos_sdk.DataType.String),
                zelos_sdk.TraceEventFieldMetadata("serial_number", zelos_sdk.DataType.String),
                zelos_sdk.TraceEventFieldMetadata("firmware_version", zelos_sdk.DataType.String),
                zelos_sdk.TraceEventFieldMetadata("ocpp_version", zelos_sdk.DataType.String),
            ],
        }

    def _build_event_templates(
        self,
    ) -> dict[str, list[zelos_sdk.TraceEventFieldMetadata]]:
        """Build per-charger event schemas, overlaying any user node-map."""
        templates = self._default_event_templates()
        if not self.node_map or not self.node_map.events:
            return templates

        for event_name, nodes in self.node_map.events.items():
            # charger_health is fleet-level; the node map cannot override it
            if event_name == "charger_health" or not nodes:
                continue
            fields: list[zelos_sdk.TraceEventFieldMetadata] = []
            for node in nodes:
                dtype = SDK_DATATYPE_MAP.get(node.datatype)
                if dtype is None:
                    continue
                fields.append(zelos_sdk.TraceEventFieldMetadata(node.name, dtype, node.unit))
            if fields:
                templates[event_name] = fields
        return templates

    def _init_trace_source(self) -> None:
        """Initialize Zelos trace source and register fleet-level events."""
        source_name = self.node_map.name if self.node_map else "ocpp"
        self._source = zelos_sdk.TraceSourceCacheLast(source_name)

        # Fleet-level event only — per-charger events register lazily on connect.
        self._source.add_event(
            "charger_health",
            [
                zelos_sdk.TraceEventFieldMetadata("total_connected", zelos_sdk.DataType.UInt8, ""),
                zelos_sdk.TraceEventFieldMetadata("total_healthy", zelos_sdk.DataType.UInt8, ""),
                zelos_sdk.TraceEventFieldMetadata("total_stale", zelos_sdk.DataType.UInt8, ""),
                zelos_sdk.TraceEventFieldMetadata("event_type", zelos_sdk.DataType.UInt8, ""),
            ],
        )
        self._source.add_value_table("charger_health", "event_type", HEALTH_EVENT_NAMES)

        self._event_templates = self._build_event_templates()

    def _register_charger(self, cp_id: str, ocpp_version: str = "1.6") -> str:
        """Resolve cp_id → trace path and register per-charger events once.

        Returns the resolved trace-path segment for this charger.
        """
        path = self._resolver.resolve(cp_id)
        if path in self._registered_paths or not self._source:
            return path

        for event_name, fields in self._event_templates.items():
            self._source.add_event(f"{path}/{event_name}", fields)

        # OCPP 1.6 and 2.0.1 use different connector status enums.
        status_table = (
            CONNECTOR_STATUS_NAMES_201 if ocpp_version == "2.0.1" else CONNECTOR_STATUS_NAMES
        )
        self._source.add_value_table(f"{path}/status", "connector_status", status_table)
        self._source.add_value_table(f"{path}/firmware", "firmware_status", FIRMWARE_STATUS_NAMES)

        self._registered_paths.add(path)
        logger.info(f"[CSMS] Registered trace path for {cp_id!r} -> ocpp/{path}/*")
        return path

    def get_charger_path(self, cp_id: str) -> str:
        """Public accessor for the resolved trace path of a charge point."""
        return self._resolver.resolve(cp_id)

    def _log_event(
        self, cp_id: str, event_name: str, values: dict[str, Any], ocpp_version: str = "1.6"
    ) -> None:
        """Log a per-charger event. Resolves the charger path and registers
        schemas lazily on first sight."""
        if not self._source:
            return
        path = self._register_charger(cp_id, ocpp_version=ocpp_version)
        self._source.log(f"{path}/{event_name}", values)

    def process_meter_values(
        self, cp_id: str, sampled_values: list[dict], connector_id: int = 1
    ) -> None:
        """Convert OCPP MeterValues to a Zelos trace event for this charger."""
        measurand_map: dict[str, float] = {}
        for sv in sampled_values:
            measurand = sv.get("measurand", "Energy.Active.Import.Register")
            try:
                measurand_map[measurand] = float(sv.get("value", "0"))
            except (ValueError, TypeError):
                continue

        values: dict[str, Any] = {"connector_id": connector_id}
        if "Energy.Active.Import.Register" in measurand_map:
            values["energy_wh"] = measurand_map["Energy.Active.Import.Register"]
        if "Power.Active.Import" in measurand_map:
            values["power_w"] = measurand_map["Power.Active.Import"]
        if "Current.Import" in measurand_map:
            values["current_a"] = measurand_map["Current.Import"]
        if "Voltage" in measurand_map:
            values["voltage_v"] = measurand_map["Voltage"]
        if "SoC" in measurand_map:
            values["soc_percent"] = measurand_map["SoC"]
        if "Temperature" in measurand_map:
            values["temperature_c"] = measurand_map["Temperature"]

        if len(values) > 1:  # more than just connector_id
            self._log_event(cp_id, "meter_values", values)
            self._meter_values_count += 1

    def log_status(
        self, cp_id: str, connector_id: int, connector_status: int, error_code: int
    ) -> None:
        """Log a connector status change for this charger."""
        self._log_event(
            cp_id,
            "status",
            {
                "connector_id": connector_id,
                "connector_status": connector_status,
                "error_code": error_code,
            },
        )

    def log_session(
        self,
        cp_id: str,
        connector_id: int,
        transaction_id: int,
        meter_start_wh: float = 0.0,
        meter_stop_wh: float = 0.0,
    ) -> None:
        """Log a session (transaction) event for this charger."""
        self._log_event(
            cp_id,
            "session",
            {
                "connector_id": connector_id,
                "transaction_id": transaction_id,
                "meter_start_wh": meter_start_wh,
                "meter_stop_wh": meter_stop_wh,
            },
        )

    def log_firmware(self, cp_id: str, status: str, request_id: int | None = None) -> None:
        """Log a firmware status event for this charger."""
        self._log_event(
            cp_id,
            "firmware",
            {
                "firmware_status": FIRMWARE_STATUS_MAP.get(status, 0),
                "request_id": request_id or 0,
            },
        )

    def log_info(
        self,
        cp_id: str,
        vendor: str = "",
        model: str = "",
        serial_number: str = "",
        firmware_version: str = "",
        ocpp_version: str = "",
    ) -> None:
        """Log a one-shot identity event for this charger (from BootNotification)."""
        self._log_event(
            cp_id,
            "info",
            {
                "vendor": vendor,
                "model": model,
                "serial_number": serial_number,
                "firmware_version": firmware_version,
                "ocpp_version": ocpp_version,
            },
            ocpp_version=ocpp_version or "1.6",
        )

    def log_charger_health(self, event_type: int) -> None:
        """Log a charger health trace event."""
        if not self._source:
            return
        now = time.time()
        total = len(self._charge_points)
        stale = sum(
            1
            for h in self._charge_points.values()
            if (now - h.last_heartbeat) > HEARTBEAT_STALE_THRESHOLD_S
        )
        self._source.charger_health.log(
            total_connected=total,
            total_healthy=total - stale,
            total_stale=stale,
            event_type=event_type,
        )

    async def _poll_trigger_meter_values(self) -> None:
        """Periodically send TriggerMessage(MeterValues) to all connected CPs."""
        while self._running:
            await asyncio.sleep(self.poll_interval)
            for cp_id, handler in list(self._charge_points.items()):
                try:
                    await handler.trigger_meter_values()
                    logger.debug(f"[CSMS] Triggered MeterValues on {cp_id}")
                except Exception as e:
                    logger.warning(f"[CSMS] TriggerMessage failed for {cp_id}: {e}")

    async def _poll_charger_health(self) -> None:
        """Periodically emit charger health trace events."""
        while self._running:
            await asyncio.sleep(self.poll_interval)
            try:
                self.log_charger_health(HEALTH_EVENT_PERIODIC)
            except Exception as e:
                logger.warning(f"[CSMS] Health poll failed: {e}")

    async def _on_connect(self, websocket: ServerConnection) -> None:
        """Handle a new charge point WebSocket connection."""
        # Extract charge point ID from the path
        path = websocket.request.path if hasattr(websocket, "request") else "/"
        cp_id = path.strip("/").split("/")[-1] if path.strip("/") else "unknown"

        # Detect OCPP version from negotiated subprotocol
        try:
            requested = websocket.request.headers.get_all("Sec-WebSocket-Protocol")
        except AttributeError:
            requested = []

        flat_protocols = [p.strip() for r in requested for p in r.split(",")]

        use_201 = "ocpp2.0.1" in flat_protocols

        ocpp_version = "2.0.1" if use_201 else "1.6"
        logger.info(f"[CSMS] Charge point connected: {cp_id} (version={ocpp_version})")

        if use_201:
            handler = ChargePointHandler201(cp_id, websocket, self)
        else:
            handler = ChargePointHandler16(cp_id, websocket, self)

        self._charge_points[cp_id] = handler

        # Register per-charger schemas eagerly so the right value-table
        # variant (1.6 vs 2.0.1) is bound before any messages arrive.
        self._register_charger(cp_id, ocpp_version=ocpp_version)
        self.log_charger_health(HEALTH_EVENT_CONNECT)

        try:
            await handler.start()
        except Exception as e:
            logger.info(f"[CSMS] Charge point disconnected: {cp_id} ({e})")
        finally:
            self._charge_points.pop(cp_id, None)
            self.log_charger_health(HEALTH_EVENT_DISCONNECT)
            logger.info(f"[CSMS] Charge point removed: {cp_id}")

    def start(self) -> None:
        """Start the CSMS (initialize trace source)."""
        self._running = True
        self._init_trace_source()
        logger.info("OcppCsms started")

    def stop(self) -> None:
        """Stop the CSMS."""
        self._running = False
        logger.info("OcppCsms stopped")

    def run(self) -> None:
        """Run the CSMS WebSocket server (blocking)."""
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        """Async main loop: start WebSocket server and listen for connections."""
        self._loop = asyncio.get_running_loop()

        ssl_context = None
        if self.ssl_cert_file and self.ssl_key_file:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(self.ssl_cert_file, self.ssl_key_file)
            scheme = "wss"
        else:
            scheme = "ws"

        logger.info(f"[CSMS] Starting WebSocket server on {self.host}:{self.port}")

        async with serve(
            self._on_connect,
            self.host,
            self.port,
            subprotocols=["ocpp1.6", "ocpp2.0.1"],
            ssl=ssl_context,
        ):
            logger.info(f"[CSMS] Listening on {scheme}://{self.host}:{self.port}")
            poll_task = asyncio.create_task(self._poll_trigger_meter_values())
            health_task = asyncio.create_task(self._poll_charger_health())
            try:
                while self._running:
                    await asyncio.sleep(1.0)
            finally:
                poll_task.cancel()
                health_task.cancel()

    def _call_on_loop(self, coro, timeout: float = 10.0):
        """Run an async coroutine on the CSMS event loop from an action thread."""
        if not self._loop or self._loop.is_closed():
            raise RuntimeError("CSMS event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # =========================================================================
    # SDK Actions
    # =========================================================================

    @zelos_sdk.action("Get Status", "List connected charge points and CSMS health")
    def get_status(self) -> dict[str, Any]:
        cp_list = []
        for cp_id, handler in self._charge_points.items():
            connectors = {}
            for cid, conn in handler._connectors.items():
                connectors[str(cid)] = {
                    "status": conn.status,
                    "error_code": conn.error_code,
                    "active_transaction": conn.active_transaction_id,
                }

            cp_list.append(
                {
                    "id": cp_id,
                    "trace_path": self._resolver.resolve(cp_id),
                    "ocpp_version": handler.ocpp_version,
                    "connectors": connectors,
                    "firmware_status": handler.firmware_status,
                    "connected_seconds": round(time.time() - handler.connected_at),
                    "last_heartbeat_seconds_ago": round(time.time() - handler.last_heartbeat),
                }
            )

        return {
            "running": self._running,
            "host": self.host,
            "port": self.port,
            "charge_points": cp_list,
            "connected_count": len(cp_list),
            "meter_values_received": self._meter_values_count,
            "total_transactions": self._next_transaction_id,
        }

    @zelos_sdk.action(
        "Remote Start Transaction", "Send RemoteStartTransaction to a connected charge point"
    )
    @zelos_sdk.action.text("cp_id", title="Charge Point ID")
    @zelos_sdk.action.text("id_tag", title="ID Tag")
    def remote_start_action(self, cp_id: str, id_tag: str) -> dict[str, Any]:
        handler = self._charge_points.get(cp_id)
        if not handler:
            return {"error": f"Charge point '{cp_id}' not connected", "success": False}

        try:
            response = self._call_on_loop(handler.remote_start(id_tag))
            return {
                "cp_id": cp_id,
                "id_tag": id_tag,
                "status": response.status,
                "success": response.status == "Accepted",
            }
        except Exception as e:
            return {"error": str(e), "success": False}

    @zelos_sdk.action(
        "Remote Stop Transaction", "Send RemoteStopTransaction to a connected charge point"
    )
    @zelos_sdk.action.text("cp_id", title="Charge Point ID")
    @zelos_sdk.action.integer("transaction_id", title="Transaction ID", minimum=1)
    def remote_stop_action(self, cp_id: str, transaction_id: int) -> dict[str, Any]:
        handler = self._charge_points.get(cp_id)
        if not handler:
            return {"error": f"Charge point '{cp_id}' not connected", "success": False}

        try:
            response = self._call_on_loop(handler.remote_stop(transaction_id))
            return {
                "cp_id": cp_id,
                "transaction_id": transaction_id,
                "status": response.status,
                "success": response.status == "Accepted",
            }
        except Exception as e:
            return {"error": str(e), "success": False}

    @zelos_sdk.action("Reset Charge Point", "Send Reset command to a connected charge point")
    @zelos_sdk.action.text("cp_id", title="Charge Point ID")
    @zelos_sdk.action.select(
        "reset_type",
        title="Reset Type",
        choices=["Soft", "Hard"],
        default="Soft",
    )
    def reset_action(self, cp_id: str, reset_type: str) -> dict[str, Any]:
        handler = self._charge_points.get(cp_id)
        if not handler:
            return {"error": f"Charge point '{cp_id}' not connected", "success": False}

        try:
            response = self._call_on_loop(handler.reset(reset_type))
            return {
                "cp_id": cp_id,
                "reset_type": reset_type,
                "status": response.status,
                "success": response.status == "Accepted",
            }
        except Exception as e:
            return {"error": str(e), "success": False}

    @zelos_sdk.action("Trigger MeterValues", "Request immediate meter readings from a charge point")
    @zelos_sdk.action.text("cp_id", title="Charge Point ID")
    def trigger_meter_values_action(self, cp_id: str) -> dict[str, Any]:
        handler = self._charge_points.get(cp_id)
        if not handler:
            return {"error": f"Charge point '{cp_id}' not connected", "success": False}

        try:
            response = self._call_on_loop(handler.trigger_meter_values())
            return {
                "cp_id": cp_id,
                "status": response.status,
                "success": response.status == "Accepted",
            }
        except Exception as e:
            return {"error": str(e), "success": False}

    @zelos_sdk.action("Update Firmware", "Send firmware update to a charge point")
    @zelos_sdk.action.text("cp_id", title="Charge Point ID")
    @zelos_sdk.action.text("location", title="Firmware URL")
    def update_firmware_action(self, cp_id: str, location: str) -> dict[str, Any]:
        handler = self._charge_points.get(cp_id)
        if not handler:
            return {"error": f"Charge point '{cp_id}' not connected", "success": False}

        self._next_firmware_request_id += 1
        request_id = self._next_firmware_request_id

        try:
            response = self._call_on_loop(handler.update_firmware(location, request_id))
            # v16 UpdateFirmware returns empty response (no status field)
            if hasattr(response, "status"):
                status = response.status
                success = status == "Accepted"
            else:
                status = "Accepted"
                success = True
            return {
                "cp_id": cp_id,
                "location": location,
                "request_id": request_id,
                "status": status,
                "success": success,
            }
        except Exception as e:
            return {"error": str(e), "success": False}
