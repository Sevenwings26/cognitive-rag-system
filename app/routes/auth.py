# app/routes/auth.py
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.dependencies import get_db, get_current_user
from modules.auth.domain.models import UserRole
from modules.auth.domain.tokens import TokenData
from modules.auth.services.token_service import verify_password, create_access_token
from modules.auth.repositories.user_repository import UserRepository
from modules.governance.services.audit_logger import AuditLogger
from modules.auth.validators import validate_enterprise_email
from app.schemas.auth import (
    UserRegisterPayload, UserLoginPayload, TokenResponse,
    TenantRegistrationPayload, UserProfileResponse
)

router = APIRouter(prefix="/enterprise/auth", tags=["Enterprise Identity & Access"])

@router.post("/register-tenant", response_model=TokenResponse)
def register_tenant(payload: TenantRegistrationPayload, db: Session = Depends(get_db)):
    slug = payload.org_slug.lower().strip()
    existing_org = UserRepository.get_org_by_slug(db, slug)
    if existing_org:
        raise HTTPException(status_code=400, detail=f"Tenant slug '{slug}' is already registered")

    admin_email = validate_enterprise_email(payload.admin_email)
    existing_user = UserRepository.get_by_email(db, admin_email)
    if existing_user:
        raise HTTPException(status_code=400, detail=f"User with email '{admin_email}' already exists")

    # 1. Create Organization
    org = UserRepository.create_org(db, name=payload.org_name.strip(), slug=slug)

    # 2. Create Default Department
    dept_code = payload.department_code.lower().strip() if payload.department_code else "eng"
    dept_name = payload.department_name.strip() if payload.department_name else "Engineering"
    dept = UserRepository.create_department(db, org_id=org.id, name=dept_name, slug=dept_code)

    # 3. Create Root SuperAdmin
    user = UserRepository.create_user(
        db=db,
        org_id=org.id,
        email=admin_email,
        password=payload.admin_password,
        full_name=payload.admin_name.strip(),
        role=UserRole.SUPER_ADMIN,
        department_id=dept.id
    )

    token_data = {
        "sub": user.id,
        "email": user.email,
        "org_id": user.org_id,
        "dept_id": user.department_id,
        "role": user.role.value
    }
    access_token = create_access_token(data=token_data)

    AuditLogger.log(
        db=db,
        org_id=user.org_id,
        user_id=user.id,
        action="TENANT_REGISTERED",
        resource_type="ORGANIZATION",
        resource_id=org.id,
        details={"org_name": org.name, "admin_email": user.email}
    )

    try:
        from modules.observatory.buffer import get_observatory_buffer
        get_observatory_buffer().record_active_session(user.id)
    except Exception:
        pass

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        user_id=user.id,
        org_id=user.org_id,
        department_id=user.department_id,
        role=user.role.value
    )

@router.post("/register", response_model=TokenResponse)
def register_user(payload: UserRegisterPayload, db: Session = Depends(get_db)):
    org = UserRepository.get_org_by_id(db, payload.org_id)
    if not org:
        raise HTTPException(status_code=400, detail="Organization not found")

    email = validate_enterprise_email(payload.email)
    existing_user = UserRepository.get_by_email(db, email)
    if existing_user:
        raise HTTPException(status_code=400, detail=f"User with email '{email}' already exists")

    role = UserRole(payload.role) if hasattr(UserRole, payload.role) else UserRole.MEMBER
    user = UserRepository.create_user(
        db=db,
        org_id=payload.org_id,
        email=email,
        password=payload.password,
        full_name=payload.full_name.strip() if payload.full_name else None,
        role=role,
        department_id=payload.department_id
    )

    token_data = {
        "sub": user.id,
        "email": user.email,
        "org_id": user.org_id,
        "dept_id": user.department_id,
        "role": user.role.value
    }
    access_token = create_access_token(data=token_data)

    AuditLogger.log(
        db=db,
        org_id=user.org_id,
        user_id=user.id,
        action="USER_REGISTERED",
        resource_type="USER",
        resource_id=user.id,
        details={"email": user.email, "role": user.role.value}
    )

    try:
        from modules.observatory.buffer import get_observatory_buffer
        get_observatory_buffer().record_active_session(user.id)
    except Exception:
        pass

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        user_id=user.id,
        org_id=user.org_id,
        department_id=user.department_id,
        role=user.role.value
    )

@router.post("/login", response_model=TokenResponse)
def login_user(payload: UserLoginPayload, db: Session = Depends(get_db)):
    user = UserRepository.get_by_email(db, payload.email.strip())
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"}
        )

    token_data = {
        "sub": user.id,
        "email": user.email,
        "org_id": user.org_id,
        "dept_id": user.department_id,
        "role": user.role.value
    }
    access_token = create_access_token(data=token_data)

    AuditLogger.log(
        db=db,
        org_id=user.org_id,
        user_id=user.id,
        action="USER_LOGIN",
        resource_type="AUTH",
        resource_id=user.id
    )

    try:
        from modules.observatory.buffer import get_observatory_buffer
        get_observatory_buffer().record_active_session(user.id)
    except Exception:
        pass

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        user_id=user.id,
        org_id=user.org_id,
        department_id=user.department_id,
        role=user.role.value
    )

@router.post("/logout")
def logout_user(current_user: TokenData = Depends(get_current_user)):
    try:
        from modules.observatory.buffer import get_observatory_buffer
        get_observatory_buffer().remove_active_session(current_user.user_id)
    except Exception:
        pass
    return {"status": "ok", "message": "Session terminated successfully"}

@router.get("/me", response_model=UserProfileResponse)
def get_current_user_profile(current_user: TokenData = Depends(get_current_user)):
    return UserProfileResponse(
        user_id=current_user.user_id,
        email=current_user.email or "",
        org_id=current_user.org_id,
        department_id=current_user.department_id,
        role=current_user.role
    )
