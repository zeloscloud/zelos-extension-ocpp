"""OCPP charge point variable map for Zelos trace event definitions.

The node map format uses events to group OCPP measurands semantically:

{
  "name": "my_charger",
  "events": {
    "meter_values": [
      {"name": "power_w", "address": "Power.Active.Import", "datatype": "float32", "unit": "W"}
    ],
    "status": [
      {"name": "connector_status", "address": "Status", "datatype": "uint8"}
    ]
  }
}

Event names become Zelos trace events. Node names become fields within those events.
The "address" field maps to OCPP measurand names for identification.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VALID_DATATYPES = {
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
}


@dataclass
class Node:
    """A single OCPP variable definition."""

    address: str
    name: str
    datatype: str = "float32"
    unit: str = ""
    scale: float = 1.0
    writable: bool | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.datatype not in VALID_DATATYPES:
            msg = f"Invalid datatype '{self.datatype}'. Must be one of {sorted(VALID_DATATYPES)}"
            raise ValueError(msg)
        if not self.address:
            msg = "Node address cannot be empty"
            raise ValueError(msg)


@dataclass
class NodeMap:
    """Collection of OCPP variable definitions organized by events."""

    events: dict[str, list[Node]] = field(default_factory=dict)
    name: str = "ocpp"
    description: str = ""

    @classmethod
    def from_file(cls, path: str | Path) -> NodeMap:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Node map file not found: {path}")

        with path.open() as f:
            data = json.load(f)

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeMap:
        events: dict[str, list[Node]] = {}

        for event_name, nodes_data in data.get("events", {}).items():
            nodes = []
            for node_data in nodes_data:
                node = Node(
                    address=node_data["address"],
                    name=node_data["name"],
                    datatype=node_data.get("datatype", "float32"),
                    unit=node_data.get("unit", ""),
                    scale=node_data.get("scale", 1.0),
                    writable=node_data.get("writable"),
                    description=node_data.get("description", ""),
                )
                nodes.append(node)
            events[event_name] = nodes

        return cls(
            events=events,
            name=data.get("name", "ocpp"),
            description=data.get("description", ""),
        )

    @property
    def nodes(self) -> list[Node]:
        return [n for nodes in self.events.values() for n in nodes]

    @property
    def event_names(self) -> list[str]:
        return list(self.events.keys())

    def get_event(self, event_name: str) -> list[Node]:
        return self.events.get(event_name, [])

    def get_by_name(self, name: str) -> Node | None:
        for nodes in self.events.values():
            for node in nodes:
                if node.name == name:
                    return node
        return None

    def get_by_address(self, address: str) -> Node | None:
        for nodes in self.events.values():
            for node in nodes:
                if node.address == address:
                    return node
        return None
