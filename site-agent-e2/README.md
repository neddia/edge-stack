# site-agent-e2: the host side of the E2 protocol

These files are the edge computer's half of the conversation with the
[Edge101 field nodes](https://github.com/neddia/edge101-lorawan-node). They're
copied from site-agent as it was on **Feb 1, 2026**, the day before LoRaWAN was
removed from it. They're here for reading. They import site-agent internals
(`app.config`, `app.settings`, the Modbus profile loader) that aren't included,
so they won't run on their own. The optimizer isn't included either.
`Dispatcher.apply_group_hz` only receives the fan speed the optimizer chose.

| File | What it does |
|---|---|
| `services/e2_setup.py` | Message IDs and encoders: columnar EXEC and POLLS bodies, SAFEHZ, BUS, the name dictionary, CTRL masks, bundles |
| `services/edge_link.py` | Builds the 9-byte E2 header and splits a message into fragments; sizes fragments from the LoRa data rate; TCP transport; ChirpStack gRPC downlinks |
| `services/lora_uplink.py` | MQTT client for ChirpStack uplinks; learns each node's payload limit from its uplink data rate |
| `services/dispatcher.py` | One lock per node. Picks TCP or LoRa (with optional fallback), reassembles LoRa fragments, unpacks bundles, matches replies to requests by msg-id |
| `services/provision.py` | Turns site config into each node's poll schedule, safe-speed commands and permission masks. It hashes the plan and only sends what changed, as one bundle |
| `services/ingester.py` | Listens for reports and status the nodes send unprompted; drops duplicates and replies to our own requests; writes telemetry and safe-speed events to InfluxDB |
| `services/e2_heartbeat.py` | Pings every node on a timer so its safe-speed fallback doesn't fire while the host is healthy |
| `services/edge_discovery.py`, `edge_registry.py` | Scan the LAN for nodes, identify them by MAC or DevEUI, and cache their IPs |
| `services/edge_roles.py` | Primary/secondary node pairs per fan bank |
| `services/lora_telemetry.py` | RSSI, SNR, data rate and join events into InfluxDB |
| `tests/` | LoRa and TCP integration tests with a fake ChirpStack and a TCP echo server. `test_live_tcp_e2.py` sends a STATUS request to a real node |

Two changes from the original: the LoRa test uses a placeholder DevEUI, and one
test that drove the HTTP API router (not included) was removed from
`test_integration_tcp.py`.
