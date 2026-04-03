# Zelos extension for OCPP (Open Charge Point Protocol)

## Features

- ⚡ **OCPP 1.6 + 2.0.1 support** — Automatic version detection via WebSocket subprotocol negotiation
- 📊 **Real-time meter values** — Energy, power, current, voltage, SoC, and temperature streamed as Zelos traces
- 🔌 **Multi-connector tracking** — Per-connector status, transactions, and meter data
- 🩺 **Charger health monitoring** — Fleet-level connect/disconnect/stale visibility via periodic health events
- 🔒 **TLS/WSS support** — Encrypted charge point connections with PEM certificates
- 🛠️ **Remote actions** — Start/stop transactions, trigger meter values, reset, firmware update from the Zelos App
- 🚗 **Demo mode** — Built-in simulated charge point for testing without hardware

## Quick Start

1. **Install** the extension from the Zelos App
2. **Configure** your CSMS host, port, and optional TLS certificates
3. **Start** the extension — it begins listening for OCPP charge point connections
4. **Connect** your charge points to `ws://<host>:<port>/<charge-point-id>`
5. **View** real-time charging data in your Zelos App

## Configuration

All configuration is managed through the Zelos App settings interface.

### Required Settings
- **Listen Host** — Host address for the CSMS WebSocket server (default: `0.0.0.0`)
- **Listen Port** — Port for the CSMS WebSocket server (default: `9000`)

### Optional Settings
- **Device Map File** — JSON file for custom charge point variable mapping
- **Poll Interval** — How often to send TriggerMessage for meter values (default: 10s, range: 1–300s)
- **TLS Certificate File** — PEM certificate for WSS/TLS
- **TLS Key File** — PEM private key for WSS/TLS
- **Log Level** — Logging verbosity (DEBUG, INFO, WARNING, ERROR)

## Trace Events

| Event | Fields | Description |
|-------|--------|-------------|
| `meter_values` | connector_id, energy_wh, power_w, current_a, voltage_v, soc_percent, temperature_c | Periodic meter readings |
| `status` | connector_id, connector_status, error_code | Connector status changes |
| `session` | connector_id, transaction_id, meter_start_wh, meter_stop_wh | Transaction lifecycle |
| `firmware` | firmware_status, request_id | Firmware update progress |
| `charger_health` | total_connected, total_healthy, total_stale, event_type | Fleet health snapshots |

## Actions

The extension provides several actions accessible from the Zelos App:

- **Get Status** — View connected charge points, connector states, firmware status, and statistics
- **Remote Start Transaction** — Start a charging session on a specific charge point
- **Remote Stop Transaction** — Stop an active charging session
- **Trigger MeterValues** — Request immediate meter readings from a charge point
- **Reset Charge Point** — Send a soft or hard reset command
- **Update Firmware** — Trigger a firmware update on a charge point

## What is OCPP?

[Open Charge Point Protocol (OCPP)](https://www.openchargealliance.org/) is the standard communication protocol between EV charging stations and central management systems. This extension acts as a CSMS (Central System Management Server) that charge points connect to via WebSocket.

## Development

Want to contribute or modify this extension? See [CONTRIBUTING.md](CONTRIBUTING.md) for the complete developer guide.

## Links

- **Repository**: [github.com/zeloscloud/zelos-extension-ocpp](https://github.com/zeloscloud/zelos-extension-ocpp)
- **Issues**: [Report bugs or request features](https://github.com/zeloscloud/zelos-extension-ocpp/issues)

## CLI Usage

The extension includes a command-line interface for advanced use cases. See [cli/README.md](cli/README.md) for details.

## Support

For help and support:
- 📖 [Zelos Documentation](https://docs.zeloscloud.io)
- 🐛 [GitHub Issues](https://github.com/zeloscloud/zelos-extension-ocpp/issues)
- 📧 help@zeloscloud.io

## License

MIT License - see [LICENSE](LICENSE) for details.

---

**Built with [Zelos](https://zeloscloud.io)**
