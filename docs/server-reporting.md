# Server reporting: what comes over MQTT, what still uses REST

State as of 2026-11-01. The topic contract is [`contracts/mqtt-topics.md`](../contracts/mqtt-topics.md) (version 1.1); this page is the overview.

## Over MQTT (live)

`sjc-agent` runs in Docker on the hub, the GPU server and the GPU desktop, and publishes to the Mosquitto broker on the hub. The laptop's own agent publishes the same topic shapes. Every topic is retained, so a new subscriber gets the latest value at once. MQTT keeps no history: the metrics recorder stores it ([metrics-history.md](metrics-history.md)).

| Topic `sjc/<host>/…` | What | Every | Hub | GPU server | GPU desktop | Laptop |
|---|---|---|---|---|---|---|
| `status` | `online` / `offline` (last will) | on change | ✓ | ✓ | ✓ | ✓ |
| `system` | CPU %, load, RAM (total/used/available), swap, disk and network I/O rates, NIC link + speed, ECC error counts, RAM bandwidth, true power peak, uptime | 5 s | ✓ | ✓ | ✓ | ✓ (no load or swap; adds battery and CPU package power) |
| `sensors` | every temperature and fan reading | 5 s | ✓ | ✓ | ✓ (case fans via the nct6775 driver) | ✓ (Lenovo CPU/GPU/board temps and fans; needs the agent elevated) |
| `disks` | space per mount (`remote` = NFS, `same_as` = bind mount: don't double-count), software RAID | 60 s | ✓ | ✓ | ✓ | ✓ (internal drive only) |
| `health` | SMART per drive (including bays behind the RAID controller), kernel I/O errors in the last hour, ECC | 10 min | ✓ | ✓ | ✓ | – |
| `gpu` | per card: temperature, utilization, memory, fan, power and its limits, clocks, PCIe | 2 s (laptop 5 s) | – | ✓ | ✓ | ✓ |
| `idrac` | BMC fans (RPM), BMC temperatures, PSUs, power | 5 s | ✓ | ✓ | – | – |
| `power` | wall watts: now, 5-s peak, 60-s max/avg/min, 5-min max | 5 s | ✓ | ✓ | – | – |
| `safety` | fan-control state (mode, fan %, emergency, per-component temps), Emergency limits, manual hold | 5 s | ✓ | ✓ | – | – |
| `runlevel` | run profile, power throttle, scaler | 5 s | ✓ | ✓ | ✓ | – |

Hosts in topics are the agents' short names (the `<HOST>` in their settings), e.g. `srv1`, `srv2`, `laptop`.

## Still REST

| What | Where | Why it isn't MQTT (yet) |
|---|---|---|
| The classic dashboard page (`:8090`) and its controls: run profile, Quick Cool, meeting mode, GPU switch, power target, throttle | hub `app.py`, `/api/*` | It still collects by pulling the dash-agents (`:8091` on the GPU server and the GPU desktop), reading the laptop's push (HTTP or the reports table), and its own local readers on the hub. Switching it to read the MQTT topics is the dashboard owner's change; everything it shows is now on MQTT |
| Control actions (fans, GPU limit, modes) | `:8090` | The core safety layer's controls stay on `:8090` by design. MQTT `cmd/*` topics are deprecated (contract section "Commands") |
| GPU broker: work claims, heartbeats, results, runner orders and reports | hub `:8099` (agent API) | Job completion stays out of MQTT for now (owner, 2026-10-31) |
| Task runner status | each server `:8098` | Read-only per-server page |
| GPU broker status | hub `:8099/status` | Read-only JSON: queues, GPUs, loaded models, reservation |
| Stored history (graphs) | hub `:8108` (`/series`, `/metrics`) | The metrics recorder subscribes to MQTT and stores one point per minute (24 h) and per hour (forever): [metrics-history.md](metrics-history.md) |

The newer dashboard backend (`:8100`) already subscribes to `sjc/+/+`, so it receives the new topics without changes.

## Reading the MQTT data

- **Broker:** `<hub address>:1883`, LAN only, no username or password (anonymous on purpose; port 1883 is never exposed to the internet).
- **Everything, live, from any LAN machine with the Mosquitto clients:**
  ```bash
  mosquitto_sub -h <hub address> -t 'sjc/#' -v
  ```
  One host or one topic: `-t 'sjc/srv2/#'`, `-t 'sjc/+/disks'`. Add `-C 1` to take one message and exit.
- **On the hub without installing anything:**
  ```bash
  sudo docker exec sjc-mosquitto-1 mosquitto_sub -h 127.0.0.1 -t 'sjc/+/health' -v
  ```
- **From Python** (`pip install "paho-mqtt>=2.1,<3"`):
  ```python
  import json
  import paho.mqtt.client as mqtt

  def on_message(c, u, msg):
      print(msg.topic, json.loads(msg.payload) if msg.payload[:1] in b"{[" else msg.payload.decode())

  c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
  c.on_message = on_message
  c.connect("<hub address>", 1883)
  c.subscribe("sjc/+/system")
  c.loop_forever()
  ```
- **Freshness:** every payload has `at` (Unix seconds). A retained message stays after a host goes down, so check `status` (`offline` is the last will) or the age of `at`.
