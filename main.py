#!/usr/bin/env python3
"""Zelos OCPP Extension - CLI entry point.

This module provides the command-line interface for the OCPP extension.
It can run in several modes:

1. App mode (default): Loads configuration from config.json when run from Zelos App
2. Demo mode: Uses built-in charge point simulator (no hardware required)
3. CLI trace mode: Direct command-line usage with explicit arguments

Supports OCPP 1.6 and 2.0.1 with automatic version detection, multi-connector
charge points, firmware updates, and optional TLS/WSS.

Examples:
    # Run from Zelos App (uses config.json)
    uv run main.py

    # Demo mode (simulated charge point)
    uv run main.py demo

    # CLI trace mode
    uv run main.py trace --port 9000

    # CLI trace mode with TLS
    uv run main.py trace --port 9000 --ssl-cert cert.pem --ssl-key key.pem
"""

from __future__ import annotations

import logging
import signal
import sys
from types import FrameType
from typing import TYPE_CHECKING

import rich_click as click
import zelos_sdk
from zelos_sdk.hooks.logging import TraceLoggingHandler

if TYPE_CHECKING:
    from zelos_extension_ocpp.client import OcppCsms

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Global client reference for shutdown handler
_client: OcppCsms | None = None


def shutdown_handler(signum: int, frame: FrameType | None) -> None:
    """Handle graceful shutdown on SIGTERM or SIGINT."""
    logger.info("Shutting down...")
    if _client:
        _client.stop()
    sys.exit(0)


def set_shutdown_client(client: OcppCsms) -> None:
    """Set the client for shutdown handling."""
    global _client
    _client = client


@click.group(invoke_without_command=True)
@click.option("--demo", is_flag=True, help="Run in demo mode with simulated charge point")
@click.pass_context
def cli(ctx: click.Context, demo: bool) -> None:
    """Zelos OCPP Extension - EV charging station management via OCPP 1.6 + 2.0.1.

    When run without a subcommand, starts in app mode using configuration
    from the Zelos App (config.json).

    Use --demo flag or 'demo' subcommand for simulated charge point.
    Use 'trace' subcommand for direct CLI access without Zelos App.
    """
    ctx.ensure_object(dict)
    ctx.obj["shutdown_handler"] = set_shutdown_client
    ctx.obj["demo"] = demo

    if ctx.invoked_subcommand is None:
        run_app_mode(ctx, demo=demo)


def run_app_mode(ctx: click.Context, demo: bool = False) -> None:
    """Run in app mode with Zelos SDK initialization."""
    handler = TraceLoggingHandler("zelos_extension_ocpp_logger")
    logging.getLogger().addHandler(handler)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    from zelos_extension_ocpp.cli.app import run_app_mode as _run_app_mode

    _run_app_mode(demo=demo)


@cli.command()
@click.pass_context
def demo(ctx: click.Context) -> None:
    """Run demo mode with simulated charge point.

    Starts the CSMS WebSocket server and connects a simulated charge
    point that generates realistic meter values. No hardware required.
    """
    run_app_mode(ctx, demo=True)


@cli.command()
@click.option("--host", "-h", type=str, default="0.0.0.0", help="Listen host")
@click.option("--port", "-p", type=int, default=9000, help="Listen port")
@click.option("--interval", "-i", type=float, default=10.0, help="Poll interval in seconds")
@click.option("--ssl-cert", type=click.Path(exists=True), default=None, help="TLS cert (PEM)")
@click.option("--ssl-key", type=click.Path(exists=True), default=None, help="TLS key (PEM)")
@click.pass_context
def trace(
    ctx: click.Context,
    host: str,
    port: int,
    interval: float,
    ssl_cert: str | None,
    ssl_key: str | None,
) -> None:
    """Trace OCPP charge points from command line.

    Starts the CSMS WebSocket server (OCPP 1.6 + 2.0.1) and waits for
    charge points to connect. Supports optional TLS/WSS.

    \b
    Examples:
        # Start CSMS on default port
        uv run main.py trace

        # Start on custom port
        uv run main.py trace --port 8080

        # Start with TLS
        uv run main.py trace --ssl-cert cert.pem --ssl-key key.pem
    """
    from zelos_extension_ocpp.client import OcppCsms

    handler = TraceLoggingHandler("zelos_extension_ocpp_logger")
    logging.getLogger().addHandler(handler)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    global _client
    _client = OcppCsms(
        host=host,
        port=port,
        poll_interval=interval,
        ssl_cert_file=ssl_cert,
        ssl_key_file=ssl_key,
    )

    # Register actions BEFORE init() -- SDK requires this ordering
    zelos_sdk.actions_registry.register(_client)
    zelos_sdk.init(name="zelos_extension_ocpp", actions=True)

    scheme = "wss" if ssl_cert else "ws"
    logger.info(f"Starting OCPP CSMS on {scheme}://{host}:{port}")
    _client.start()
    _client.run()


if __name__ == "__main__":
    cli()
