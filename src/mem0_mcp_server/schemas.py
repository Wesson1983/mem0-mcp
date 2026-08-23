"""Shared Pydantic models for the Mem0 MCP server (OSS REST API edition)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


def _validate_iso_date(value: str | None) -> str | None:
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


class AddMemoryArgs(BaseModel):
    text: str | None = Field(
        None, description="Simple sentence to remember; converted into a user message when set."
    )
    messages: list[ToolMessage] | None = Field(
        None,
        description=(
            "Explicit role/content history for durable storage. Provide this OR `text`."
        ),
    )
    user_id: str | None = Field(None, description="Override for the Mem0 user ID.")
    agent_id: str | None = Field(None, description="Optional agent identifier.")
    run_id: str | None = Field(None, description="Optional run identifier.")
    metadata: dict[str, Any] | None = Field(None, description="Opaque metadata to persist.")
    expiration_date: str | None = Field(
        None, description="Expiration date in YYYY-MM-DD format."
    )
    infer: bool | None = Field(
        None, description="Whether to extract facts from messages. Defaults to True."
    )
    memory_type: str | None = Field(
        None, description="Type of memory to store (e.g. 'core')."
    )
    prompt: str | None = Field(
        None, description="Custom prompt to use for fact extraction."
    )

    _check_expiration = field_validator("expiration_date")(_validate_iso_date)


class SearchMemoriesArgs(BaseModel):
    query: str = Field(..., description="Describe what you want to find.")
    filters: dict[str, Any] | None = Field(
        None, description="Additional filter clauses; user_id is injected automatically."
    )
    top_k: int | None = Field(
        None, ge=1, le=1000, description="Optional maximum number of matches."
    )
    threshold: float | None = Field(
        None, description="Minimum similarity score for results."
    )
    explain: bool | None = Field(
        None, description="Include score details for each search result."
    )
    show_expired: bool | None = Field(None, description="Include expired memories.")


class GetMemoriesArgs(BaseModel):
    user_id: str | None = Field(None, description="Filter by user ID.")
    agent_id: str | None = Field(None, description="Filter by agent ID.")
    run_id: str | None = Field(None, description="Filter by run ID.")
    top_k: int | None = Field(
        None, ge=1, le=1000, description="Maximum number of memories to return (max 1000)."
    )
    show_expired: bool | None = Field(None, description="Include expired memories.")


class UpdateMemoryArgs(BaseModel):
    text: str | None = Field(None, description="New content to update the memory with.")
    metadata: dict[str, Any] | None = Field(None, description="Metadata to update.")
    expiration_date: str | None = Field(
        None, description="Expiration date in YYYY-MM-DD format."
    )

    _check_expiration = field_validator("expiration_date")(_validate_iso_date)


class DeleteAllArgs(BaseModel):
    user_id: str | None = Field(None, description="User scope to delete; defaults to server user.")
    agent_id: str | None = Field(None, description="Optional agent scope filter.")
    run_id: str | None = Field(None, description="Optional run scope filter.")


class DeleteEntitiesArgs(BaseModel):
    user_id: str | None = Field(None, description="Delete this user and all related memories.")
    agent_id: str | None = Field(None, description="Delete this agent and its memories.")
    run_id: str | None = Field(None, description="Delete this run and its memories.")
