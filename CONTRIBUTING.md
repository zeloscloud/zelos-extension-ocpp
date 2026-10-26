# Contributing

## Prerequisites

- [Zelos](https://zeloscloud.io) 26.0.10 or later
- [Zelos CLI](https://docs.zeloscloud.io/reference/cli/) 0.1.11 or later
- [just](https://github.com/casey/just)
- [uv](https://docs.astral.sh/uv/)

## Layout

`extension.toml` describes everything the extension contributes:

| Part | Files | Manifest |
| ---- | ----- | -------- |
| Agent runtime | `main.py`, the Python package, `pyproject.toml`, `config.schema.json` | `[agent]`, `[config]` |

| Path                                 | Purpose                                              |
| ------------------------------------ | ---------------------------------------------------- |
| `main.py`                            | Entry point; the `demo` and `trace` commands         |
| `zelos_extension_ocpp/client.py`     | The OCPP 1.6 and 2.0.1 central system (CSMS)         |
| `zelos_extension_ocpp/ocpp_map.py`   | Device map of charge point variables to trace events |
| `zelos_extension_ocpp/charger_id.py` | Charge point ID resolution and trace-path names      |
| `zelos_extension_ocpp/cli/app.py`    | App mode: loads `config.json` and starts the CSMS    |
| `zelos_extension_ocpp/demo/`         | Simulated charge points and the demo device map      |

`cli/README.md` documents the `trace` and `demo` commands.

## Commands

| Command                | Description                                        |
| ---------------------- | -------------------------------------------------- |
| `just install`         | Install dependencies and the pre-commit hooks      |
| `just format`          | Format code with ruff                              |
| `just check`           | Check formatting and lint with ruff                |
| `just test`            | Run tests with pytest                              |
| `just package`         | Package for the Zelos marketplace                  |
| `just release VERSION` | Bump version, check, test, commit, tag             |
| `just clean`           | Remove build output, caches and packages           |
| `just run`             | Run the extension against `./config.json`          |
| `just demo`            | Run with a built-in simulated charge point         |

`just --list` shows every recipe. `just install` installs the pre-commit hooks in
`.pre-commit-config.yaml`.

`just run` reads `./config.json`. Every setting has a default, so no file is needed to listen on
`0.0.0.0:9000`. Point a charge point at that port, or run the simulated one with `just demo`
(the CSMS and `CP_SIM_001`). Both connect to the agent at `ZELOS_AGENT_URL`, else
`http://localhost:2300`. Demo mode is a command-line flag only: the agent config has no demo
setting.

The tests listen on ports 19000 to 19003 and 19993. The TLS
tests create a self-signed certificate with `openssl`.

## Run it in Zelos

```bash
zelos extensions install-local .
zelos extensions start local.ocpp --config '{"port": 9000}'
zelos extensions logs local.ocpp
```

A local install runs from your project directory, so you do not install again after a code
change:

- Agent runtime: stop and start the extension. After a dependency change, stop it and run
  `zelos extensions reinstall local.ocpp`.
- `extension.toml`: run `zelos extensions install-local .` again.

## Packaging

```bash
just package
```

The archive lands next to `extension.toml` as `<name>-<version>.tar.gz`, where `<name>` is the
manifest name in lowercase with every other run of characters turned into one hyphen. It holds
`extension.toml`, the README, the icon, the config schema and the paths listed in
`[package] paths`. With an agent runtime it also holds `pyproject.toml`, `uv.lock` and
`actions.json`, the list of actions that run while the extension is stopped.

## Release

```bash
just release 1.0.0
git push --follow-tags
```

The release workflow checks, tests and packages the extension, then attaches the archive to a
GitHub release.
