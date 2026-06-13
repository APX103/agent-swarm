"""Registry Pydantic models for Agent registration and info."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from typing import Optional


class AgentCapabilities(BaseModel):
    """Describes what an agent can do."""
    input_modes: list[str] = Field(default_factory=list, description="Accepted input modalities (e.g. text, image)")
    output_modes: list[str] = Field(default_factory=list, description="Output modalities the agent produces")
    tools: list[str] = Field(default_factory=list, description="Names of tools the agent exposes")


class AgentRegistration(BaseModel):
    """Payload used when an agent registers itself."""
    name: str = Field(..., description="Human-readable agent name")
    endpoint: str = Field(..., description="URL where the agent can be reached")
    protocol: str = Field(default="http", description="Communication protocol (http, grpc, websocket)")
    skills: list[str] = Field(default_factory=list, description="Skill tags for discovery")
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    version: str = Field(default="0.1.0", description="Agent version string")
    heartbeat_interval: Optional[int] = Field(None, description="Agent-specific heartbeat interval override (seconds)")


class AgentInfo(BaseModel):
    """Full agent record stored in Redis."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16], description="Unique agent instance id")
    name: str
    endpoint: str
    protocol: str = "http"
    skills: list[str] = Field(default_factory=list)
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    version: str = "0.1.0"
    status: str = "online"
    registered_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_heartbeat: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    instance_id: str = Field(default_factory=lambda: uuid.uuid4().hex, description="Opaque instance identifier for dedup")
