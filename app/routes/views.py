# app/routes/views.py
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import get_db, get_optional_user, resolve_effective_user
from modules.auth.domain.tokens import TokenData
from modules.governance.repositories.chat_repository import ChatRepository
from modules.governance.repositories.document_repository import DocumentRepository

router = APIRouter(tags=["UI Views"])

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

@router.get("/login", response_class=HTMLResponse)
@router.get("/enterprise/login", response_class=HTMLResponse)
def render_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@router.get("/enterprise/register", response_class=HTMLResponse)
def render_register(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@router.get("/enterprise/dashboard", response_class=HTMLResponse)
def render_dashboard(request: Request):
    return templates.TemplateResponse('enterprise_dashboard.html', {"request": request})

@router.get("/enterprise/observatory", response_class=HTMLResponse)
def render_observatory(request: Request):
    return templates.TemplateResponse('observatory.html', {"request": request})

@router.get("/", response_class=HTMLResponse)
def render_main_workspace(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[TokenData] = Depends(get_optional_user)
):
    # Do not leak server-cached or guest sessions in SSR; client-side JS loads authenticated user sessions
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "session_id": "",
            "sessions": [],
            "messages": [],
            "documents": []
        }
    )

@router.get("/chat/{session_id}", response_class=HTMLResponse)
def render_chat_session(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db),
    current_user: Optional[TokenData] = Depends(get_optional_user)
):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "session_id": session_id,
            "sessions": [],
            "messages": [],
            "documents": []
        }
    )
