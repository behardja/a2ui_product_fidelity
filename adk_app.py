"""Agent Engine / `adk web` entry point."""

import vertexai  # noqa: F401  (ensures vertexai is importable in the runtime)
from vertexai.agent_engines import AdkApp
from google.adk.apps import App

try:
    from .agent import root_agent
except ImportError:
    from agent import root_agent

adk_app = AdkApp(
    app=App(name="product_fidelity_a2ui", root_agent=root_agent),
    enable_tracing=True,
)
