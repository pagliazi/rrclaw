FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl && \
    rm -rf /var/lib/apt/lists/*

# Install Hermes Agent
RUN git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /hermes-agent && \
    cd /hermes-agent && pip install -e .

# Install bridge
COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY . .

ENV HERMES_AGENT_PATH=/hermes-agent
ENV PYTHONPATH=/app:/hermes-agent

CMD ["python", "-m", "bridge.server", "--config", "config.example.yaml"]
