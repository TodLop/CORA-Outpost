"""
Backend operations documentation routes.

Access model:
- Minecraft admin (owner/manager/global admin): always allowed
- Staff: only with ops:backend_docs:view
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.core.auth import require_auth, is_staff
from app.core.minecraft_access import is_minecraft_admin_user
from app.core.config import TEMPLATES_DIR
from app.services import backend_docs, permissions as permissions_service

router = APIRouter(prefix="/minecraft/backend-docs", tags=["BackendDocs"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _serialize_summary(doc: backend_docs.BackendDocSummary) -> dict:
    return {
        "slug": doc.slug,
        "title": doc.title,
        "audience": doc.audience,
        "owner": doc.owner,
        "last_reviewed_at": doc.last_reviewed_at,
        "tags": list(doc.tags),
        "source_path": doc.source_path,
        "updated_at": doc.updated_at,
    }


async def require_backend_docs_access(request: Request) -> dict:
    """
    Backend docs access:
    - Minecraft admins: always allowed
    - Staff: requires ops:backend_docs:view permission
    """
    user_info = await require_auth(request)
    if is_minecraft_admin_user(user_info):
        return user_info

    if not is_staff(user_info):
        raise HTTPException(status_code=403, detail="Staff access required")

    email = user_info.get("email", "")
    if not permissions_service.has_permission(email, "ops:backend_docs:view"):
        raise HTTPException(status_code=403, detail="Permission denied: ops:backend_docs:view")
    return user_info


@router.get("/api/docs")
async def get_docs_index(user_info: dict = Depends(require_backend_docs_access)):
    user_is_minecraft_admin = is_minecraft_admin_user(user_info)
    docs = backend_docs.list_docs(is_admin_user=user_is_minecraft_admin)
    return JSONResponse({
        "status": "ok",
        "docs": [_serialize_summary(doc) for doc in docs],
    })


@router.get("/api/docs/{slug}")
async def get_doc(slug: str, user_info: dict = Depends(require_backend_docs_access)):
    user_is_minecraft_admin = is_minecraft_admin_user(user_info)
    doc = backend_docs.get_doc(slug, is_admin_user=user_is_minecraft_admin)
    if not doc:
        return JSONResponse({"status": "error", "error": "Document not found"}, status_code=404)
    return JSONResponse({
        "status": "ok",
        "doc": {
            "slug": doc.slug,
            "title": doc.title,
            "audience": doc.audience,
            "owner": doc.owner,
            "last_reviewed_at": doc.last_reviewed_at,
            "tags": list(doc.tags),
            "source_path": doc.source_path,
            "updated_at": doc.updated_at,
            "raw": doc.raw,
            "html": doc.html,
        },
    })


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def backend_docs_home(
    request: Request,
    slug: Optional[str] = Query(default=None),
    user_info: dict = Depends(require_backend_docs_access),
):
    user_is_minecraft_admin = is_minecraft_admin_user(user_info)
    docs = backend_docs.list_docs(is_admin_user=user_is_minecraft_admin)
    if not docs:
        raise HTTPException(status_code=404, detail="No backend docs found")

    selected_slug = slug or docs[0].slug
    selected_doc = backend_docs.get_doc(selected_slug, is_admin_user=user_is_minecraft_admin)
    if not selected_doc:
        raise HTTPException(status_code=404, detail="Document not found")

    return templates.TemplateResponse(
        request,
        "operations/backend_docs.html",
        {
            "user_info": user_info,
            "is_admin": user_is_minecraft_admin,
            "is_minecraft_admin": user_is_minecraft_admin,
            "docs": docs,
            "selected_doc": selected_doc,
        },
    )


@router.get("/{slug}", response_class=HTMLResponse)
async def backend_doc_detail(
    request: Request,
    slug: str,
    user_info: dict = Depends(require_backend_docs_access),
):
    user_is_minecraft_admin = is_minecraft_admin_user(user_info)
    docs = backend_docs.list_docs(is_admin_user=user_is_minecraft_admin)
    if not docs:
        raise HTTPException(status_code=404, detail="No backend docs found")

    selected_doc = backend_docs.get_doc(slug, is_admin_user=user_is_minecraft_admin)
    if not selected_doc:
        raise HTTPException(status_code=404, detail="Document not found")

    return templates.TemplateResponse(
        request,
        "operations/backend_docs.html",
        {
            "user_info": user_info,
            "is_admin": user_is_minecraft_admin,
            "is_minecraft_admin": user_is_minecraft_admin,
            "docs": docs,
            "selected_doc": selected_doc,
        },
    )
