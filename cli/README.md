# CLI Usage

The extension includes a command-line interface for advanced use cases and development. No installation required — just use `uv run`:

> **Tip:** Run `pip install .` to install and use `zelos-extension-ocpp <args>` from anywhere.

## OCPP Tracing (no Zelos App required)

```bash
# Start CSMS on default port (9000), wait for charge points to connect
uv run main.py trace

# Custom port and poll interval
uv run main.py trace --port 8080 --interval 5

# With TLS/WSS
uv run main.py trace --port 9000 --ssl-cert cert.pem --ssl-key key.pem
```

## Demo Mode (built-in simulator)

```bash
# Start CSMS + simulated charge point (no hardware required)
uv run main.py demo

# Or via the --demo flag
uv run main.py --demo
```

Demo mode starts the CSMS and connects a simulated OCPP 1.6 charge point (`CP_SIM_001`) that runs through a full charging cycle with realistic meter values.

## App Mode (Zelos App integration)

```bash
# Run with config from Zelos App (default when launched by the agent)
uv run main.py
```

This is the mode used when the extension is started from the Zelos App. Configuration is loaded from `config.json` written by the agent.
