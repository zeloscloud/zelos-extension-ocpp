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
- **Charger Aliases File** — JSON map of OCPP charge-point IDs to friendly trace paths (see below)
- **Poll Interval** — How often to send TriggerMessage for meter values (default: 10s, range: 1–300s)
- **TLS Certificate File** — PEM certificate for WSS/TLS
- **TLS Key File** — PEM private key for WSS/TLS
- **Log Level** — Logging verbosity (DEBUG, INFO, WARNING, ERROR)

## Trace Layout

Each connected charger gets its own subtree under the OCPP source, identified
by the WebSocket-path `cp_id` (sanitized) or an operator-supplied alias.
Fleet-level health stays at the root.

```
ocpp/
├── charger_health                 # fleet totals: connected / healthy / stale
└── <charger_path>/
    ├── info                       # one-shot on BootNotification
    ├── status                     # connector status changes
    ├── session                    # transaction start/stop
    ├── firmware                   # firmware update progress
    └── meter_values               # periodic readings
```

| Event | Fields | Description |
|-------|--------|-------------|
| `<cp>/meter_values` | connector_id, energy_wh, power_w, current_a, voltage_v, soc_percent, temperature_c | Periodic meter readings |
| `<cp>/status` | connector_id, connector_status, error_code | Connector status changes |
| `<cp>/session` | connector_id, transaction_id, meter_start_wh, meter_stop_wh | Transaction lifecycle |
| `<cp>/firmware` | firmware_status, request_id | Firmware update progress |
| `<cp>/info` | vendor, model, serial_number, firmware_version, ocpp_version | Charger identity from BootNotification |
| `charger_health` | total_connected, total_healthy, total_stale, event_type | Fleet snapshots (root) |

### Charger Aliases

Raw OCPP `cp_id` values are sanitized for use as trace-path segments (dots,
colons, slashes, and whitespace become underscores). To give chargers
human-friendly names — or organize them hierarchically by site/bay —
provide a JSON aliases file:

```json
{
  "AABBCCDDEEFF": "depot_north/bay_3",
  "ABB_EVB_2024_00471": "garage/charger_a",
  "192.168.1.42": "lab/bench_psu"
}
```

Aliases may contain `/` to nest chargers under a site or fleet — the Zelos
app will render the hierarchy in its trace tree. Aliases are themselves
sanitized per-segment.

If no alias matches and the raw `cp_id` sanitizes to nothing usable, the
extension assigns a monotonic fallback path (`cp_0`, `cp_1`, …) and logs a
warning.

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
