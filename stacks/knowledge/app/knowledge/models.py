from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

EMBEDDING_DIMENSION = 3072


def normalize_embedding(value: Any) -> list[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("embedding must be a sequence of floats")

    embedding = [float(component) for component in value]
    if len(embedding) != EMBEDDING_DIMENSION:
        raise ValueError(f"embedding must contain exactly {EMBEDDING_DIMENSION} floats")

    return embedding


def _normalize_required_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


class KnowledgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Document(KnowledgeModel):
    id: UUID | None = None
    source_path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    ingested_at: datetime | None = None

    @field_validator("source_path", "title", "content_hash")
    @classmethod
    def validate_required_text_fields(cls, value: str) -> str:
        return _normalize_required_text(value)


class Chunk(KnowledgeModel):
    id: UUID | None = None
    document_id: UUID
    chunk_index: int = Field(ge=0)
    content: str = Field(min_length=1)
    embedding: list[float] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return _normalize_required_text(value)

    @field_validator("embedding", mode="before")
    @classmethod
    def validate_embedding(cls, value: Any) -> list[float] | None:
        if value is None:
            return None
        return normalize_embedding(value)

    @field_validator("metadata", mode="before")
    @classmethod
    def normalize_metadata(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, Mapping):
            return dict(value)
        raise TypeError("metadata must be a mapping")


class SearchResult(KnowledgeModel):
    score: float = Field(ge=0.0, le=1.0)
    document: Document
    chunk: Chunk


class IngestResult(KnowledgeModel):
    documents_processed: int = Field(ge=0)
    chunks_created: int = Field(ge=0)
    documents_skipped: int = Field(ge=0)


class DirectoryIngestResult(IngestResult):
    files_found: int = Field(ge=0)
    files_failed: int = Field(ge=0)
    documents_deleted: int = Field(ge=0)
