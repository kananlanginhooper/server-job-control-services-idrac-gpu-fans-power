# sjc-agent: build from the repo root (context ../..), see services/sjc-agent/compose.yaml
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ipmitool && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/sjc-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lib/sjc_hw /tmp/hw
RUN pip install --no-cache-dir /tmp/hw && rm -rf /tmp/hw
# the shared .env loader (<HOST>_KEY, then KEY); the container shares the host's hostname (network_mode: host)
COPY lib/svcenv.py .
COPY services/sjc-agent/sjc_agent.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "sjc_agent.py"]
