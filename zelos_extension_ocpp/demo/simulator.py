"""Simulated OCPP 1.6 and 2.0.1 charge points for demo mode.

Uses the `ocpp` library to simulate charge points that connect to the
extension's CSMS WebSocket server, send BootNotification, StatusNotification,
and simulate charging sessions with periodic MeterValues.
Supports multi-connector, firmware update handling, and TLS/WSS.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import ssl
import time
import uuid
from typing import Any

from ocpp.routing import on
from ocpp.v16 import ChargePoint as Cp16
from ocpp.v16 import call as call16
from ocpp.v16 import call_result as call_result16
from ocpp.v16.enums import (
    Action as Action16,
)
from ocpp.v16.enums import (
    AuthorizationStatus,
    ChargePointStatus,
    FirmwareStatus,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    ResetType,
    TriggerMessageStatus,
)
from ocpp.v201 import ChargePoint as Cp201
from ocpp.v201 import call as call201
from ocpp.v201 import call_result as call_result201
from ocpp.v201.enums import (
    Action as Action201,
)
from ocpp.v201.enums import (
    AuthorizationStatusEnumType as AuthStatus201,
)
from ocpp.v201.enums import (
    BootReasonEnumType,
    MessageTriggerEnumType,
    RequestStartStopStatusEnumType,
    ResetEnumType,
    TransactionEventEnumType,
    TriggerReasonEnumType,
    UpdateFirmwareStatusEnumType,
)
from ocpp.v201.enums import (
    ConnectorStatusEnumType as ConnStatus201,
)
from ocpp.v201.enums import (
    FirmwareStatusEnumType as FwStatus201,
)
from ocpp.v201.enums import (
    RegistrationStatusEnumType as RegStatus201,
)

try:
    from websockets.asyncio.client import connect
except ImportError:
    from websockets import connect

logger = logging.getLogger(__name__)


def _compute_meter_values(
    charging: bool, start_time: float, energy_total: float, soc: float
) -> tuple[dict[str, float], float, float]:
    """Compute simulated meter values. Returns (values_dict, energy_delta, new_soc)."""
    t = time.time() - start_time

    if charging:
        voltage = 230.0 + 5.0 * math.sin(t * 0.1) + random.gauss(0, 0.5)
        current = 32.0 * (0.9 + 0.1 * math.sin(t * 0.05)) + random.gauss(0, 0.2)
        power = voltage * current
        energy_delta = power * 1.0 / 3600.0  # 1 second in Wh
        new_soc = min(100.0, soc + energy_delta / 600.0)  # ~60kWh battery
        temperature = 35.0 + 5.0 * math.sin(t * 0.02) + random.gauss(0, 0.3)
    else:
        voltage = 230.0 + random.gauss(0, 0.5)
        current = 0.0
        power = 0.0
        energy_delta = 0.0
        new_soc = soc
        temperature = 25.0 + random.gauss(0, 0.2)

    return (
        {
            "energy_wh": round(energy_total + energy_delta, 2),
            "power_w": round(power, 1),
            "current_a": round(current, 2),
            "voltage_v": round(voltage, 1),
            "soc_percent": round(new_soc, 1),
            "temperature_c": round(temperature, 1),
        },
        energy_delta,
        new_soc,
    )


# =============================================================================
# OCPP 1.6 Simulator
# =============================================================================


class SimulatedChargePoint(Cp16):
    """A simulated OCPP 1.6 charge point."""

    def __init__(self, id: str, connection):
        super().__init__(id, connection)
        self.start_time = time.time()
        self.energy_total = 0.0
        self.transaction_id: int | None = None
        self.charging = False
        self.soc = 20.0
        self._trigger_meter_event = asyncio.Event()

    # ---- Handlers for CSMS-initiated messages ----

    @on(Action16.remote_start_transaction)
    def on_remote_start(self, id_tag: str, **kwargs):
        logger.info(f"[SIM] Remote start requested with id_tag={id_tag}")
        asyncio.ensure_future(self._start_charging_session(id_tag))
        return call_result16.RemoteStartTransaction(
            status=RemoteStartStopStatus.accepted,
        )

    @on(Action16.remote_stop_transaction)
    def on_remote_stop(self, transaction_id: int, **kwargs):
        logger.info(f"[SIM] Remote stop requested for transaction_id={transaction_id}")
        if self.transaction_id == transaction_id:
            self.charging = False
            return call_result16.RemoteStopTransaction(
                status=RemoteStartStopStatus.accepted,
            )
        return call_result16.RemoteStopTransaction(
            status=RemoteStartStopStatus.rejected,
        )

    @on(Action16.reset)
    def on_reset(self, type: str, **kwargs):
        logger.info(f"[SIM] Reset requested: {type}")
        if type == ResetType.hard:
            self.charging = False
            self.transaction_id = None
        return call_result16.Reset(status=ResetStatus.accepted)

    @on(Action16.trigger_message)
    def on_trigger_message(self, requested_message: str, **kwargs):
        logger.info(f"[SIM] TriggerMessage requested: {requested_message}")
        if requested_message == "MeterValues":
            self._trigger_meter_event.set()
            return call_result16.TriggerMessage(status=TriggerMessageStatus.accepted)
        return call_result16.TriggerMessage(status=TriggerMessageStatus.accepted)

    @on(Action16.update_firmware)
    def on_update_firmware(self, location: str, retrieve_date: str, **kwargs):
        logger.info(f"[SIM] UpdateFirmware requested: location={location}")
        asyncio.ensure_future(self._firmware_update_sequence())
        return call_result16.UpdateFirmware()

    # ---- Simulation logic ----

    async def _firmware_update_sequence(self) -> None:
        """Simulate firmware update status sequence."""
        statuses = [
            FirmwareStatus.downloading,
            FirmwareStatus.downloaded,
            FirmwareStatus.installing,
            FirmwareStatus.installed,
        ]
        for status in statuses:
            await asyncio.sleep(1.0)
            await self.call(call16.FirmwareStatusNotification(status=status))
            logger.info(f"[SIM] FirmwareStatusNotification: {status}")

    async def send_boot_notification(self) -> bool:
        """Send BootNotification and return True if accepted."""
        response = await self.call(
            call16.BootNotification(
                charge_point_model="ZelosSim",
                charge_point_vendor="Zelos",
                firmware_version="1.0.0",
                charge_point_serial_number="SIM-001",
            )
        )
        if response.status == RegistrationStatus.accepted:
            logger.info(f"[SIM] BootNotification accepted, heartbeat={response.interval}s")
            return True
        logger.warning(f"[SIM] BootNotification rejected: {response.status}")
        return False

    async def send_status_notification(
        self,
        connector_id: int = 1,
        status: str = ChargePointStatus.available,
        error_code: str = "NoError",
    ) -> None:
        """Send a StatusNotification."""
        await self.call(
            call16.StatusNotification(
                connector_id=connector_id,
                error_code=error_code,
                status=status,
            )
        )
        logger.info(f"[SIM] StatusNotification: connector={connector_id} status={status}")

    async def send_meter_values(self, connector_id: int = 1) -> None:
        """Send current MeterValues for a connector."""
        values, energy_delta, self.soc = _compute_meter_values(
            self.charging, self.start_time, self.energy_total, self.soc
        )
        self.energy_total += energy_delta

        energy_measurand = "Energy.Active.Import.Register"
        sampled_values = [
            {"value": str(values["energy_wh"]), "measurand": energy_measurand, "unit": "Wh"},
            {"value": str(values["power_w"]), "measurand": "Power.Active.Import", "unit": "W"},
            {"value": str(values["current_a"]), "measurand": "Current.Import", "unit": "A"},
            {"value": str(values["voltage_v"]), "measurand": "Voltage", "unit": "V"},
            {"value": str(values["soc_percent"]), "measurand": "SoC", "unit": "Percent"},
            {"value": str(values["temperature_c"]), "measurand": "Temperature", "unit": "Celsius"},
        ]

        await self.call(
            call16.MeterValues(
                connector_id=connector_id,
                meter_value=[
                    {
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "sampled_value": sampled_values,
                    }
                ],
            )
        )

    async def _start_charging_session(self, id_tag: str, connector_id: int = 1) -> None:
        """Simulate a full charging session lifecycle."""
        await self.send_status_notification(
            connector_id=connector_id, status=ChargePointStatus.preparing
        )

        # Authorize
        response = await self.call(call16.Authorize(id_tag=id_tag))
        if response.id_tag_info["status"] != AuthorizationStatus.accepted:
            logger.warning("[SIM] Authorization rejected")
            await self.send_status_notification(
                connector_id=connector_id, status=ChargePointStatus.available
            )
            return

        # Start transaction
        meter_start = round(self.energy_total)
        response = await self.call(
            call16.StartTransaction(
                connector_id=connector_id,
                id_tag=id_tag,
                meter_start=meter_start,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        )
        self.transaction_id = response.transaction_id
        self.charging = True
        logger.info(f"[SIM] Transaction started: id={self.transaction_id}")

        await self.send_status_notification(
            connector_id=connector_id, status=ChargePointStatus.charging
        )

        # Send meter values periodically while charging
        while self.charging:
            await self.send_meter_values(connector_id=connector_id)
            try:
                await asyncio.wait_for(self._trigger_meter_event.wait(), timeout=1.0)
                self._trigger_meter_event.clear()
            except TimeoutError:
                pass

            # Auto-stop when SoC reaches 80% (for demo brevity)
            if self.soc >= 80.0:
                self.charging = False

        # Stop transaction
        meter_stop = round(self.energy_total)
        await self.call(
            call16.StopTransaction(
                meter_stop=meter_stop,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                transaction_id=self.transaction_id,
            )
        )
        logger.info(
            f"[SIM] Transaction stopped: id={self.transaction_id}, "
            f"energy={meter_stop - meter_start}Wh"
        )
        self.transaction_id = None

        await self.send_status_notification(
            connector_id=connector_id, status=ChargePointStatus.finishing
        )
        await asyncio.sleep(1)
        await self.send_status_notification(
            connector_id=connector_id, status=ChargePointStatus.available
        )

    async def run_demo_cycle(self, stop_event: asyncio.Event | None = None) -> None:
        """Run the full demo lifecycle: boot, status, then start sessions on connector 1 and 2."""
        accepted = await self.send_boot_notification()
        if not accepted:
            return

        # Connector 1: Available
        await self.send_status_notification(connector_id=1, status=ChargePointStatus.available)
        # Connector 2: Available
        await self.send_status_notification(connector_id=2, status=ChargePointStatus.available)
        await asyncio.sleep(1)

        # Start a charging session on connector 1
        coro1 = self._start_charging_session("DEMO_TAG_001", connector_id=1)
        session1 = asyncio.ensure_future(coro1)

        # Short delay then start a brief session on connector 2
        await asyncio.sleep(2)
        session2 = asyncio.ensure_future(self._run_connector2_session())

        await session1
        await session2

        # After sessions, keep sending heartbeat-level meter values
        while stop_event is None or not stop_event.is_set():
            await self.send_meter_values(connector_id=1)
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event else asyncio.sleep(5.0),
                    timeout=5.0,
                )
                if stop_event and stop_event.is_set():
                    break
            except TimeoutError:
                pass

    async def _run_connector2_session(self) -> None:
        """Run a short session on connector 2 for multi-connector demonstration."""
        saved_soc = self.soc
        saved_charging = self.charging
        saved_txn = self.transaction_id

        # Use separate state for connector 2
        self.soc = 50.0
        self.charging = False

        await self.send_status_notification(connector_id=2, status=ChargePointStatus.preparing)

        response = await self.call(call16.Authorize(id_tag="DEMO_TAG_002"))
        if response.id_tag_info["status"] != AuthorizationStatus.accepted:
            await self.send_status_notification(connector_id=2, status=ChargePointStatus.available)
            self.soc = saved_soc
            self.charging = saved_charging
            self.transaction_id = saved_txn
            return

        meter_start = round(self.energy_total)
        response = await self.call(
            call16.StartTransaction(
                connector_id=2,
                id_tag="DEMO_TAG_002",
                meter_start=meter_start,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        )
        txn_id = response.transaction_id

        await self.send_status_notification(connector_id=2, status=ChargePointStatus.charging)

        # Short session: just 3 meter value cycles
        for _ in range(3):
            await self.send_meter_values(connector_id=2)
            await asyncio.sleep(1.0)

        meter_stop = round(self.energy_total)
        await self.call(
            call16.StopTransaction(
                meter_stop=meter_stop,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                transaction_id=txn_id,
            )
        )

        await self.send_status_notification(connector_id=2, status=ChargePointStatus.finishing)
        await asyncio.sleep(0.5)
        await self.send_status_notification(connector_id=2, status=ChargePointStatus.available)

        # Restore connector 1 state
        self.soc = saved_soc
        self.charging = saved_charging
        self.transaction_id = saved_txn


# =============================================================================
# OCPP 2.0.1 Simulator
# =============================================================================


class SimulatedChargePoint201(Cp201):
    """A simulated OCPP 2.0.1 charge point."""

    def __init__(self, id: str, connection):
        super().__init__(id, connection)
        self.start_time = time.time()
        self.energy_total = 0.0
        self.charging = False
        self.soc = 20.0
        self.transaction_id: str | None = None
        self._trigger_meter_event = asyncio.Event()
        self._seq_no = 0

    # ---- Handlers for CSMS-initiated messages ----

    @on(Action201.request_start_transaction)
    def on_request_start(self, id_token: dict, remote_start_id: int, **kwargs):
        token = id_token.get("id_token", "unknown") if isinstance(id_token, dict) else "unknown"
        logger.info(f"[SIM201] RequestStartTransaction: id_token={token}")
        asyncio.ensure_future(self._start_charging_session(token))
        return call_result201.RequestStartTransaction(
            status=RequestStartStopStatusEnumType.accepted,
        )

    @on(Action201.request_stop_transaction)
    def on_request_stop(self, transaction_id: str, **kwargs):
        logger.info(f"[SIM201] RequestStopTransaction: txn_id={transaction_id}")
        if self.transaction_id == transaction_id:
            self.charging = False
            return call_result201.RequestStopTransaction(
                status=RequestStartStopStatusEnumType.accepted,
            )
        return call_result201.RequestStopTransaction(
            status=RequestStartStopStatusEnumType.rejected,
        )

    @on(Action201.reset)
    def on_reset(self, type: str, **kwargs):
        logger.info(f"[SIM201] Reset requested: {type}")
        if type == ResetEnumType.immediate:
            self.charging = False
            self.transaction_id = None
        return call_result201.Reset(status="Accepted")

    @on(Action201.trigger_message)
    def on_trigger_message(self, requested_message: str, **kwargs):
        logger.info(f"[SIM201] TriggerMessage requested: {requested_message}")
        if requested_message == MessageTriggerEnumType.meter_values:
            self._trigger_meter_event.set()
        return call_result201.TriggerMessage(status="Accepted")

    @on(Action201.update_firmware)
    def on_update_firmware(self, request_id: int, firmware: dict, **kwargs):
        location = firmware.get("location", "unknown") if isinstance(firmware, dict) else "unknown"
        logger.info(f"[SIM201] UpdateFirmware: request_id={request_id}, location={location}")
        asyncio.ensure_future(self._firmware_update_sequence(request_id))
        return call_result201.UpdateFirmware(status=UpdateFirmwareStatusEnumType.accepted)

    # ---- Simulation logic ----

    async def _firmware_update_sequence(self, request_id: int) -> None:
        """Simulate firmware update status sequence (2.0.1)."""
        statuses = [
            FwStatus201.downloading,
            FwStatus201.downloaded,
            FwStatus201.installing,
            FwStatus201.installed,
        ]
        for status in statuses:
            await asyncio.sleep(1.0)
            await self.call(
                call201.FirmwareStatusNotification(status=status, request_id=request_id)
            )
            logger.info(f"[SIM201] FirmwareStatusNotification: {status}")

    def _next_seq(self) -> int:
        self._seq_no += 1
        return self._seq_no

    async def send_boot_notification(self) -> bool:
        response = await self.call(
            call201.BootNotification(
                charging_station={
                    "vendor_name": "Zelos",
                    "model": "ZelosSim201",
                    "serial_number": "SIM201-001",
                    "firmware_version": "2.0.0",
                },
                reason=BootReasonEnumType.power_up,
            )
        )
        if response.status == RegStatus201.accepted:
            logger.info(f"[SIM201] BootNotification accepted, heartbeat={response.interval}s")
            return True
        logger.warning(f"[SIM201] BootNotification rejected: {response.status}")
        return False

    async def send_status_notification(
        self,
        connector_id: int = 1,
        evse_id: int = 1,
        status: str = ConnStatus201.available,
    ) -> None:
        await self.call(
            call201.StatusNotification(
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                connector_status=status,
                evse_id=evse_id,
                connector_id=connector_id,
            )
        )
        logger.info(
            f"[SIM201] StatusNotification: evse={evse_id} connector={connector_id} status={status}"
        )

    async def _send_transaction_event(
        self,
        event_type: str,
        trigger_reason: str,
        meter_value: list | None = None,
        evse_id: int = 1,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if meter_value:
            kwargs["meter_value"] = meter_value
        kwargs["evse"] = {"id": evse_id}

        return await self.call(
            call201.TransactionEvent(
                event_type=event_type,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                trigger_reason=trigger_reason,
                seq_no=self._next_seq(),
                transaction_info={"transaction_id": self.transaction_id},
                **kwargs,
            )
        )

    def _build_meter_value(self) -> list[dict]:
        """Build a v201-format meter_value list."""
        values, _, _ = _compute_meter_values(
            self.charging, self.start_time, self.energy_total, self.soc
        )
        sampled = [
            {"value": values["energy_wh"], "measurand": "Energy.Active.Import.Register"},
            {"value": values["power_w"], "measurand": "Power.Active.Import"},
            {"value": values["current_a"], "measurand": "Current.Import"},
            {"value": values["voltage_v"], "measurand": "Voltage"},
            {"value": values["soc_percent"], "measurand": "SoC"},
        ]
        return [
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "sampled_value": sampled,
            }
        ]

    async def send_meter_values(self, evse_id: int = 1) -> None:
        values, energy_delta, self.soc = _compute_meter_values(
            self.charging, self.start_time, self.energy_total, self.soc
        )
        self.energy_total += energy_delta

        sampled = [
            {"value": values["energy_wh"], "measurand": "Energy.Active.Import.Register"},
            {"value": values["power_w"], "measurand": "Power.Active.Import"},
            {"value": values["current_a"], "measurand": "Current.Import"},
            {"value": values["voltage_v"], "measurand": "Voltage"},
            {"value": values["soc_percent"], "measurand": "SoC"},
        ]

        await self.call(
            call201.MeterValues(
                evse_id=evse_id,
                meter_value=[
                    {
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "sampled_value": sampled,
                    }
                ],
            )
        )

    async def _start_charging_session(self, id_tag: str, evse_id: int = 1) -> None:
        """Simulate a full charging session lifecycle (2.0.1)."""
        await self.send_status_notification(
            connector_id=1, evse_id=evse_id, status=ConnStatus201.occupied
        )

        # Authorize
        response = await self.call(
            call201.Authorize(
                id_token={"id_token": id_tag, "type": "Central"},
            )
        )
        auth_status = (
            response.id_token_info.get("status", "Accepted")
            if isinstance(response.id_token_info, dict)
            else "Accepted"
        )
        if auth_status != AuthStatus201.accepted:
            logger.warning("[SIM201] Authorization rejected")
            await self.send_status_notification(
                connector_id=1, evse_id=evse_id, status=ConnStatus201.available
            )
            return

        self.transaction_id = str(uuid.uuid4())
        self.charging = True

        # TransactionEvent Started
        meter_start_mv = self._build_meter_value()
        await self._send_transaction_event(
            TransactionEventEnumType.started,
            TriggerReasonEnumType.authorized,
            meter_value=meter_start_mv,
            evse_id=evse_id,
        )
        logger.info(f"[SIM201] Transaction started: id={self.transaction_id}")

        # Send periodic meter values
        while self.charging:
            values, energy_delta, self.soc = _compute_meter_values(
                self.charging, self.start_time, self.energy_total, self.soc
            )
            self.energy_total += energy_delta

            mv = self._build_meter_value()
            await self._send_transaction_event(
                TransactionEventEnumType.updated,
                TriggerReasonEnumType.meter_value_periodic,
                meter_value=mv,
                evse_id=evse_id,
            )

            try:
                await asyncio.wait_for(self._trigger_meter_event.wait(), timeout=1.0)
                self._trigger_meter_event.clear()
            except TimeoutError:
                pass

            if self.soc >= 80.0:
                self.charging = False

        # TransactionEvent Ended
        meter_stop_mv = self._build_meter_value()
        await self._send_transaction_event(
            TransactionEventEnumType.ended,
            TriggerReasonEnumType.ev_departed,
            meter_value=meter_stop_mv,
            evse_id=evse_id,
        )
        logger.info(f"[SIM201] Transaction ended: id={self.transaction_id}")
        self.transaction_id = None

        await self.send_status_notification(
            connector_id=1, evse_id=evse_id, status=ConnStatus201.available
        )

    async def run_demo_cycle(self, stop_event: asyncio.Event | None = None) -> None:
        """Run the full demo lifecycle (2.0.1)."""
        accepted = await self.send_boot_notification()
        if not accepted:
            return

        await self.send_status_notification(
            connector_id=1, evse_id=1, status=ConnStatus201.available
        )
        await asyncio.sleep(1)

        await self._start_charging_session("DEMO_TAG_201", evse_id=1)

        # After the session, keep sending meter values
        while stop_event is None or not stop_event.is_set():
            await self.send_meter_values(evse_id=1)
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event else asyncio.sleep(5.0),
                    timeout=5.0,
                )
                if stop_event and stop_event.is_set():
                    break
            except TimeoutError:
                pass


# =============================================================================
# Entry point
# =============================================================================


async def run_simulated_charge_point(
    csms_url: str = "ws://localhost:9000",
    cp_id: str = "CP_SIM_001",
    stop_event: asyncio.Event | None = None,
    ocpp_version: str = "1.6",
) -> None:
    """Connect a simulated charge point to the CSMS and run a demo cycle.

    Args:
        csms_url: WebSocket URL of the CSMS (e.g., ws://localhost:9000)
        cp_id: Charge point identifier
        stop_event: Event to signal shutdown
        ocpp_version: OCPP version to use ("1.6" or "2.0.1")
    """
    url = f"{csms_url}/{cp_id}"
    logger.info(f"[SIM] Connecting to CSMS at {url} (OCPP {ocpp_version})")

    subprotocol = "ocpp2.0.1" if ocpp_version == "2.0.1" else "ocpp1.6"

    connect_kwargs: dict[str, Any] = {"subprotocols": [subprotocol]}

    # Handle TLS for wss:// URLs
    if csms_url.startswith("wss://"):
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        connect_kwargs["ssl"] = ssl_context

    async with connect(url, **connect_kwargs) as ws:
        if ocpp_version == "2.0.1":
            cp = SimulatedChargePoint201(cp_id, ws)
        else:
            cp = SimulatedChargePoint(cp_id, ws)

        listener = asyncio.ensure_future(cp.start())

        try:
            await cp.run_demo_cycle(stop_event=stop_event)
        finally:
            listener.cancel()
