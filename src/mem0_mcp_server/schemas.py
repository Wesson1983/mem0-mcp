"""Shared Pydantic models for the Mem0 MCP server (OSS REST API edition)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator


def _validate_iso_date(value: Optional[str]) -> Optional[str]:
    """Reject expiration dates the OSS server would only fail on later."""
    if value is None:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("expiration_date must be in YYYY-MM-DD format") from exc
    return value


class ToolMessage(BaseModel):
    role: str = Field(..., description="Role of the speaker, e.g., user or assistant.")
    content: str = Field(..., description="Full text of the utterance to store.")


class ConfigSchema(BaseModel):
    """Session-level overrides used when hosting via Smithery or HTTP."""

    mem0_api_key: str = Field(..., description="Mem0 OSS API key (required)")
    default_user_id: Optional[str] = Field(
        None, description="Default user_id injected into filters when unspecified."
    )
    base_url: Optional[str] = Field(
        None, description="Base URL of the mem0 OSS REST server (e.g. http://localhost:8888)."
    )


class AddMemoryArgs(BaseModel):
    text: Optional[str] = Field(
        None, description="Simple sentence to remember; converted into a user message when set."
    )
    messages: Optional[list[ToolMessage]] = Field(
        None,
        description=(
            "Explicit role/content history for durable storage. Provide this OR `text`."
        ),
    )
    user_id: Optional[str] = Field(None, description="Override for the Mem0 user ID.")
    agent_id: Optional[str] = Field(None, description="Optional agent identifier.")
    run_id: Optional[str] = Field(None, description="Optional run identifier.")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Opaque metadata to persist.")
    expiration_date: Optional[str] = Field(
        None, description="Expiration date in YYYY-MM-DD format."
    )
    infer: Optional[bool] = Field(
        None, description="Whether to extract facts from messages. Defaults to True."
    )
    memory_type: Optional[str] = Field(
        None, description="Type of memory to store (e.g. 'core')."
    )
    prompt: Optional[str] = Field(
        None, description="Custom prompt to use for fact extraction."
    )

    _check_expiration = field_validator("expiration_date")(_validate_iso_date)


class SearchMemoriesArgs(BaseModel):
    query: str = Field(..., description="Describe what you want to find.")
    filters: Optional[Dict[str, Any]] = Field(
        None, description="Additional filter clauses; user_id is injected automatically."
    )
    top_k: Optional[int] = Field(
        None, ge=1, le=1000, description="Optional maximum number of matches."
    )
    threshold: Optional[float] = Field(
        None, description="Minimum similarity score for results."
    )
    explain: Optional[bool] = Field(
        None, description="Include score details for each search result."
    )
    show_expired: Optional[bool] = Field(None, description="Include expired memories.")


class GetMemoriesArgs(BaseModel):
    user_id: Optional[str] = Field(None, description="Filter by user ID.")
    agent_id: Optional[str] = Field(None, description="Filter by agent ID.")
    run_id: Optional[str] = Field(None, description="Filter by run ID.")
    top_k: Optional[int] = Field(
        None, ge=1, le=1000, description="Maximum number of memories to return (max 1000)."
    )
    show_expired: Optional[bool] = Field(None, description="Include expired memories.")


class UpdateMemoryArgs(BaseModel):
    text: Optional[str] = Field(None, description="New content to update the memory with.")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Metadata to update.")
    expiration_date: Optional[str] = Field(
        None, description="Expiration date in YYYY-MM-DD format."
    )

    _check_expiration = field_validator("expiration_date")(_validate_iso_date)


class DeleteAllArgs(BaseModel):
    user_id: Optional[str] = Field(None, description="User scope to delete; defaults to server user.")
    agent_id: Optional[str] = Field(None, description="Optional agent scope filter.")
    run_id: Optional[str] = Field(None, description="Optional run scope filter.")


class DeleteEntitiesArgs(BaseModel):
    user_id: Optional[str] = Field(None, description="Delete this user and all related memories.")
    agent_id: Optional[str] = Field(None, description="Delete this agent and its memories.")
    run_id: Optional[str] = Field(None, description="Delete this run and its memories.")
