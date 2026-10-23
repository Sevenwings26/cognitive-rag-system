# app/dependencies.py
from typing import Optional, List, Generator, Callable
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from core.database import SessionLocal, get_db
from modules.auth.domain.tokens import TokenData
from modules.auth.services.token_service import decode_access_token
from modules.auth.repositories.user_repository import UserRepository
from modules.rag_core.providers.llm import LLMFactory
from modules.rag_core.providers.embeddings import EmbeddingFactory
from modules.rag_core.orchestrator.unified_orchestrator import UnifiedRAGOrchestrator

security_scheme = HTTPBearer(auto_error=False)

def get_orchestrator() -> UnifiedRAGOrchestrator:
    llm_service = LLMFactory.get_provider()
    embedding_service = EmbeddingFactory.get_provider()
    return UnifiedRAGOrchestrator(llm_service=llm_service, embedding_service=embedding_service)

def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: Session = Depends(get_db)
) -> TokenData:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"}
        )
    token = credentials.credentials
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        org_id = payload.get("org_id")
        role = payload.get("role")
        department_id = payload.get("dept_id")
        email = payload.get("email")

        if not user_id or not org_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

        try:
            from modules.observatory.buffer import get_observatory_buffer
            get_observatory_buffer().record_active_session(user_id)
        except Exception:
            pass

        return TokenData(
            user_id=user_id,
            org_id=org_id,
            department_id=department_id,
            role=role or "MEMBER",
            email=email
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

def get_optional_user(
    authorization: Optional[str] = Header(None)
) -> Optional[TokenData]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.split(" ")[1]
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        org_id = payload.get("org_id")
        role = payload.get("role")
        if user_id and org_id:
            try:
                from modules.observatory.buffer import get_observatory_buffer
                get_observatory_buffer().record_active_session(user_id)
            except Exception:
                pass
            return TokenData(
                user_id=user_id,
                org_id=org_id,
                department_id=payload.get("dept_id"),
                role=role or "MEMBER",
                email=payload.get("email", "")
            )
    except Exception:
        pass
    return None

def resolve_effective_user(
    current_user: Optional[TokenData],
    db: Session
) -> TokenData:
    if current_user:
        return current_user

    default_org = UserRepository.get_org_by_slug(db, "default-org")
    if not default_org:
        default_org = UserRepository.create_org(db, name="Default Workspace", slug="default-org")

    return TokenData(
        user_id=None,
        org_id=default_org.id,
        department_id=None,
        role="MEMBER",
        email="guest@workspace.local"
    )

def require_role(allowed_roles: List[str]) -> Callable:
    def role_checker(current_user: TokenData = Depends(get_current_user)) -> TokenData:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Action requires one of roles: {allowed_roles}"
            )
        return current_user
    return role_checker
