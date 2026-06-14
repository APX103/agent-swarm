"""Unified dispatch layer.

The Dispatcher is the single seam through which the orchestrator sends work to
agents, whether they are Docker-backed workers or externally-registered agents.
See ``base.py`` for the protocol and data shapes, ``backends.py`` for the concrete
backends, and ``dispatcher.py`` for the orchestrating implementation.
"""
