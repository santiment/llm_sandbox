"""llm-sandbox — provider-agnostic code-execution sandbox sidecar.

The HTTP API (``app.py``) is the contract every caller uses; ``providers/`` holds the
swappable backends (gVisor, …). Switching providers is a server-side env change and is
invisible to the backend / agent.
"""

__version__ = "0.1.0"
