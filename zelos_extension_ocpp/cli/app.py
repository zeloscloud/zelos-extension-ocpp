"""App mode runner for Zelos OCPP extension.

Handles running the extension when launched from the Zelos App
with configuration loaded from config.json. Demo mode is only
available via the CLI --demo flag for verification purposes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import zelos_sdk
from zelos_sdk.extensions import load_config

from zelos_extension_ocpp.client import OcppCsms
from zelos_extension_ocpp.ocpp_map import NodeMap

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

DEMO_HOST = "localhost"
DEMO_PORT = 9000


def start_demo_simulator(
    listening: threading.Event, port: int, stop_event: asyncio.Event, scheme: str = "ws"
) -> threading.Thread:
    """Start the demo charge point simulator in a background thread.

    The simulator connects to the CSMS once it listens.
    """
    from zelos_extension_ocpp.demo.simulator import run_simulated_charge_point

    def run_sim() -> None:
        if not listening.wait(timeout=10.0):
            logger.error("Demo simulator: CSMS server did not start in time")
            return

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                run_simulated_charge_point(
                    csms_url=f"{scheme}://{DEMO_HOST}:{port}",
                    cp_id="CP_SIM_001",
                    stop_event=stop_event,
                )
            )
        except Exception as e:
            logger.error(f"Demo simulator error: {e}")
        finally:
            loop.close()

    thread = threading.Thread(target=run_sim, daemon=True)
    thread.start()
    logger.info("Demo charge point simulator started")
    return thread


def run_app_mode(demo: bool = False) -> None:
    """Run the extension in app mode with configuration from Zelos App."""
    config = load_config()

    is_demo = demo

    # Set log level
    log_level = config.get("log_level", "INFO")
    logging.getLogger().setLevel(getattr(logging, log_level))

    host = config.get("host", "0.0.0.0")
    port = config.get("port", DEMO_PORT)

    # Load node map if provided
    node_map = None
    map_file = config.get("device_map_file")
    if map_file:
        map_path = Path(map_file)
        if map_path.exists():
            try:
                node_map = NodeMap.from_file(map_path)
                logger.info(f"Loaded node map with {len(node_map.nodes)} nodes")
            except Exception as e:
                logger.error(f"Failed to load node map: {e}")
        else:
            logger.warning(f"Node map file not found: {map_file}")

    ssl_cert = config.get("ssl_cert_file")
    ssl_key = config.get("ssl_key_file")
    aliases_file = config.get("charger_aliases_file")

    client_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "node_map": node_map,
        "poll_interval": config.get("poll_interval", 10.0),
        "ssl_cert_file": ssl_cert if ssl_cert else None,
        "ssl_key_file": ssl_key if ssl_key else None,
        "charger_aliases_file": aliases_file if aliases_file else None,
    }

    client = OcppCsms(**client_kwargs)

    zelos_sdk.actions_registry.register(client)
    zelos_sdk.init(name="OCPP", actions=True)

    client.start()

    # Start demo simulator after CSMS is about to run
    if is_demo:
        stop_event = asyncio.Event()
        scheme = "wss" if ssl_cert else "ws"
        start_demo_simulator(client.listening, port, stop_event, scheme=scheme)

    client.run()
