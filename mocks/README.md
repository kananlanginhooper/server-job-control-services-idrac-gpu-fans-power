# Mocks

Stand-ins for the live control surfaces, so tests and demos never touch real fans, power caps or GPUs.

| Mock | Stands in for | How a service uses it |
|---|---|---|
| [`dashboard_api.py`](dashboard_api.py) | The hub dashboard API (`/api/powertarget`, `/api/state`, `/api/throttle`, `/api/powercaps`, `/api/runprofile`, `/api/quickcool`, `/api/tasks`) | Point `HUB_API_URL` at it. In tests: `with MockDashboard() as d: ...`; every POST lands in `d.posts`. For a demo: `python mocks/dashboard_api.py --port 8090` |
| [`bin/ipmitool.py`](bin/ipmitool.py) | `ipmitool` on a Dell BMC: SDR types, DCMI power, `delloem powermonitor`, `sel info` | `SJC_IPMITOOL` / `IPMITOOL_CMD` = `python mocks/bin/ipmitool.py`. Writes (raw fan commands, resets) go to `MOCK_IPMI_LOG`. `MOCK_IPMI_TEMP_CPU` sets the CPU temperatures |
| [`bin/nvidia-smi.py`](bin/nvidia-smi.py) | `nvidia-smi` with one GPU | `SJC_NVIDIA_SMI` = `python mocks/bin/nvidia-smi.py`. `-pl` / `-pm` go to `MOCK_NVSMI_LOG`. `MOCK_GPU_TEMP` sets the temperature |

The MQTT topic shapes for a mock agent are in [`contracts/mqtt-topics.md`](../contracts/mqtt-topics.md).

## Tests

```bash
python -m pip install -r tests/requirements.txt
python -m pytest -q tests
```

The tests point the settings loader at an empty file (`tests/conftest.py`), so they never read the real `.env` and can't reach live hosts.
