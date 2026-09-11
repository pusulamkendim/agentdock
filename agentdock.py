#!/usr/bin/env python3
"""Compatibility entrypoint for the modular AgentDock package."""

from agentdock import *  # noqa: F401,F403 - preserve the historic facade
from agentdock.app import main


if __name__ == "__main__":
    main()
