"""Prompt Template CRUD API (issue #598).

Routes:
  GET    /api/templates              → list all (optional ?tag= filter)
  POST   /api/templates              → create
  GET    /api/templates/{id}         → get one
  PATCH  /api/templates/{id}         → partial update
  DELETE /api/templates/{id}         → delete (204)
  POST   /api/templates/{id}/render  → render with variable substitution
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import PromptTemplate, _utcnow

router = APIRouter(prefix="/api/templates", tags=["templates"])


def _template_to_dict(t: PromptTemplate) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "content": t.content or "",
        "variables": t.variables or [],
        "tags": t.tags or [],
        "createdAt": t.created_at.isoformat() if t.created_at else None,
        "updatedAt": t.updated_at.isoformat() if t.updated_at else None,
    }


class TemplateCreateBody(BaseModel):
    name: str
    content: str = ""
    variables: list[dict] | None = None
    tags: list[str] | None = None


class TemplatePatchBody(BaseModel):
    name: str | None = None
    content: str | None = None
    variables: list[dict] | None = None
    tags: list[str] | None = None


class TemplateRenderBody(BaseModel):
    variables: dict[str, str] = {}


@router.get("")
async def list_templates(
    tag: str | None = Query(default=None),
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> list[dict]:
    """Return all templates ordered by name. Optionally filter by tag."""
    rows = (await db.execute(
        select(PromptTemplate).order_by(PromptTemplate.name)
    )).scalars().all()
    result = [_template_to_dict(t) for t in rows]
    if tag:
        result = [t for t in result if tag in (t["tags"] or [])]
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_template(
    body: TemplateCreateBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Create a named prompt template. Returns 409 if name already exists."""
    existing = (await db.execute(
        select(PromptTemplate).where(PromptTemplate.name == body.name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "name_conflict", "message": f"Template '{body.name}' already exists"},
        )

    tmpl = PromptTemplate(
        name=body.name,
        content=body.content,
        variables=body.variables,
        tags=body.tags,
    )
    db.add(tmpl)
    await db.flush()
    await record_audit(db=db, type="template.create", identity=identity, detail={"name": body.name})
    return _template_to_dict(tmpl)


@router.get("/{template_id}")
async def get_template(
    template_id: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Return a single template by id."""
    tmpl = (await db.execute(
        select(PromptTemplate).where(PromptTemplate.id == template_id)
    )).scalar_one_or_none()
    if not tmpl:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return _template_to_dict(tmpl)


@router.patch("/{template_id}")
async def update_template(
    template_id: str,
    body: TemplatePatchBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Partial update. Only fields present in the request body are changed."""
    tmpl = (await db.execute(
        select(PromptTemplate).where(PromptTemplate.id == template_id)
    )).scalar_one_or_none()
    if not tmpl:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    fields = body.model_fields_set
    if "name" in fields and body.name:
        if body.name != tmpl.name:
            clash = (await db.execute(
                select(PromptTemplate).where(PromptTemplate.name == body.name)
            )).scalar_one_or_none()
            if clash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"error": "name_conflict", "message": f"Template '{body.name}' already exists"},
                )
        tmpl.name = body.name
    if "content" in fields:
        tmpl.content = body.content
    if "variables" in fields:
        tmpl.variables = body.variables
    if "tags" in fields:
        tmpl.tags = body.tags
    tmpl.updated_at = _utcnow()

    await db.flush()
    await record_audit(db=db, type="template.update", identity=identity, detail={"id": template_id})
    return _template_to_dict(tmpl)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> None:
    """Delete a template. Returns 204."""
    tmpl = (await db.execute(
        select(PromptTemplate).where(PromptTemplate.id == template_id)
    )).scalar_one_or_none()
    if not tmpl:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    await db.delete(tmpl)
    await record_audit(db=db, type="template.delete", identity=identity, detail={"id": template_id})


@router.post("/{template_id}/render")
async def render_template(
    template_id: str,
    body: TemplateRenderBody,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Render a template by substituting {{variable}} placeholders.

    Missing variables are left as-is (no error). Extra variables are ignored.
    Returns {rendered: str}.
    """
    tmpl = (await db.execute(
        select(PromptTemplate).where(PromptTemplate.id == template_id)
    )).scalar_one_or_none()
    if not tmpl:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    rendered = tmpl.content or ""
    for key, value in body.variables.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return {"rendered": rendered}
