# app/routes/governance.py
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.dependencies import get_db, get_current_user, require_role
from modules.auth.domain.tokens import TokenData
from modules.auth.domain.models import UserRole
from modules.auth.repositories.user_repository import UserRepository
from modules.auth.validators import validate_enterprise_email
from modules.governance.repositories.governance_repository import GovernanceRepository
from modules.governance.repositories.document_repository import DocumentRepository
from modules.governance.services.audit_logger import AuditLogger
from app.schemas.governance import (
    MetricsResponse, DepartmentCreatePayload, DepartmentResponse,
    UserCreateAdminPayload, UserAdminResponse,
    PersonaCreatePayload, PersonaResponse,
    PromptTemplateCreatePayload, PromptTemplateResponse,
    AuditLogResponse
)

router = APIRouter(prefix="/enterprise", tags=["Enterprise Governance & Guardrails"])

# ---------------------------------------------------------
# 1. Tenant Overview Metrics
# ---------------------------------------------------------
@router.get("/metrics", response_model=MetricsResponse)
def get_tenant_metrics(
    current_user: TokenData = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    org = UserRepository.get_org_by_id(db, current_user.org_id)
    org_name = org.name if org else "Enterprise Platform"
    org_slug = org.slug if org else "enterprise"

    users = UserRepository.list_org_users(db, current_user.org_id)
    depts = UserRepository.list_org_departments(db, current_user.org_id)
    docs = DocumentRepository.list_accessible_documents(db, current_user.org_id, None, current_user.user_id, "SUPER_ADMIN")
    jobs = DocumentRepository.list_ingestion_jobs(db, current_user.org_id)
    audits = GovernanceRepository.list_audit_logs(db, current_user.org_id, limit=500)

    return MetricsResponse(
        org_name=org_name,
        org_slug=org_slug,
        total_users=len(users),
        total_departments=len(depts),
        total_documents=len(docs),
        total_jobs=len(jobs),
        total_audits=len(audits)
    )

# ---------------------------------------------------------
# 2. Functional Department Units
# ---------------------------------------------------------
@router.get("/departments", response_model=List[DepartmentResponse])
def list_departments(
    current_user: TokenData = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    depts = UserRepository.list_org_departments(db, current_user.org_id)
    return [
        DepartmentResponse(
            id=d.id,
            name=d.name,
            code=d.slug.upper(),
            created_at=d.created_at.isoformat() if hasattr(d, "created_at") and d.created_at else ""
        )
        for d in depts
    ]

@router.post("/departments", response_model=DepartmentResponse)
def create_department(
    payload: DepartmentCreatePayload,
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db)
):
    slug = payload.code.lower().strip()
    dept = UserRepository.create_department(
        db=db,
        org_id=current_user.org_id,
        name=payload.name.strip(),
        slug=slug
    )

    AuditLogger.log(
        db=db,
        org_id=current_user.org_id,
        user_id=current_user.user_id,
        action="DEPARTMENT_CREATED",
        resource_type="DEPARTMENT",
        resource_id=dept.id,
        details={"name": dept.name, "code": dept.slug}
    )

    return DepartmentResponse(
        id=dept.id,
        name=dept.name,
        code=dept.slug.upper(),
        created_at=dept.created_at.isoformat() if hasattr(dept, "created_at") and dept.created_at else ""
    )

# ---------------------------------------------------------
# 3. User Governance & Accounts
# ---------------------------------------------------------
@router.get("/users", response_model=List[UserAdminResponse])
def list_tenant_users(
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db)
):
    users = UserRepository.list_org_users(db, current_user.org_id)
    return [
        UserAdminResponse(
            id=u.id,
            full_name=u.full_name,
            email=u.email,
            role=u.role.value if hasattr(u.role, "value") else str(u.role),
            department_id=u.department_id,
            is_active=u.is_active,
            created_at=u.created_at.isoformat() if hasattr(u, "created_at") and u.created_at else ""
        )
        for u in users
    ]

@router.post("/users", response_model=UserAdminResponse)
def provision_user(
    payload: UserCreateAdminPayload,
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db)
):
    email = validate_enterprise_email(payload.email)
    existing = UserRepository.get_by_email(db, email)
    if existing:
        raise HTTPException(status_code=400, detail=f"User with email '{email}' already exists")

    role_val = payload.role if hasattr(UserRole, payload.role) else "MEMBER"
    user_role = UserRole(role_val)

    if current_user.role == "DEPT_ADMIN" and user_role == UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="DeptAdmins cannot provision SuperAdmins")

    user = UserRepository.create_user(
        db=db,
        org_id=current_user.org_id,
        email=email,
        password=payload.password,
        full_name=payload.full_name.strip(),
        role=user_role,
        department_id=payload.department_id
    )

    AuditLogger.log(
        db=db,
        org_id=current_user.org_id,
        user_id=current_user.user_id,
        action="USER_PROVISIONED",
        resource_type="USER",
        resource_id=user.id,
        details={"email": user.email, "role": user.role.value}
    )

    return UserAdminResponse(
        id=user.id,
        full_name=user.full_name,
        email=user.email,
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
        department_id=user.department_id,
        is_active=user.is_active,
        created_at=user.created_at.isoformat() if hasattr(user, "created_at") and user.created_at else ""
    )

# ---------------------------------------------------------
# 4. Assistant Personas
# ---------------------------------------------------------
@router.get("/governance/personas", response_model=List[PersonaResponse])
@router.get("/personas", response_model=List[PersonaResponse])
def list_personas(current_user: TokenData = Depends(get_current_user), db: Session = Depends(get_db)):
    personas = GovernanceRepository.list_personas(db, current_user.org_id)
    return [
        PersonaResponse(
            id=p.id,
            name=p.name,
            description=p.description,
            system_template=p.system_instruction_template,
            department_id=None,
            is_default=False
        )
        for p in personas
    ]

@router.post("/governance/personas", response_model=PersonaResponse)
@router.post("/personas", response_model=PersonaResponse)
def create_persona(
    payload: PersonaCreatePayload,
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db)
):
    persona = GovernanceRepository.create_persona(
        db=db,
        org_id=current_user.org_id,
        name=payload.name.strip(),
        system_instruction_template=payload.system_instruction_template.strip(),
        description=payload.description.strip() if payload.description else None,
        temperature=payload.temperature
    )

    AuditLogger.log(
        db=db,
        org_id=current_user.org_id,
        user_id=current_user.user_id,
        action="PERSONA_CREATED",
        resource_type="PERSONA",
        resource_id=persona.id,
        details={"name": persona.name}
    )

    return PersonaResponse(
        id=persona.id,
        name=persona.name,
        description=persona.description,
        system_template=persona.system_instruction_template,
        department_id=None,
        is_default=False
    )

# ---------------------------------------------------------
# 5. Governed Prompt Templates
# ---------------------------------------------------------
@router.get("/governance/prompts", response_model=List[PromptTemplateResponse])
@router.get("/prompt-templates", response_model=List[PromptTemplateResponse])
def list_prompt_templates(current_user: TokenData = Depends(get_current_user), db: Session = Depends(get_db)):
    templates = GovernanceRepository.list_prompt_templates(db, current_user.org_id)
    return [
        PromptTemplateResponse(
            id=t.id,
            title=t.name,
            category="QNA",
            persona_id=None,
            department_id=None,
            user_prompt_template=t.user_prompt_template
        )
        for t in templates
    ]

@router.post("/governance/prompts", response_model=PromptTemplateResponse)
@router.post("/prompt-templates", response_model=PromptTemplateResponse)
def create_prompt_template(
    payload: PromptTemplateCreatePayload,
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db)
):
    template = GovernanceRepository.create_prompt_template(
        db=db,
        org_id=current_user.org_id,
        name=payload.title.strip(),
        user_prompt_template=payload.user_prompt_template.strip(),
        description=f"Prompt template for category {payload.category}"
    )

    AuditLogger.log(
        db=db,
        org_id=current_user.org_id,
        user_id=current_user.user_id,
        action="PROMPT_TEMPLATE_CREATED",
        resource_type="PROMPT_TEMPLATE",
        resource_id=template.id,
        details={"name": template.name}
    )

    return PromptTemplateResponse(
        id=template.id,
        title=template.name,
        category=payload.category,
        persona_id=payload.persona_id,
        department_id=payload.target_department_id,
        user_prompt_template=template.user_prompt_template
    )

# ---------------------------------------------------------
# 6. Compliance & Audit SIEM Telemetry
# ---------------------------------------------------------
@router.get("/governance/audit-logs", response_model=List[AuditLogResponse])
@router.get("/audit/logs", response_model=List[AuditLogResponse])
def list_audit_logs(
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "AUDITOR"])),
    db: Session = Depends(get_db)
):
    logs = GovernanceRepository.list_audit_logs(db, current_user.org_id, limit=100)
    return [
        AuditLogResponse(
            id=l.id,
            user_id=l.user_id,
            action=l.action,
            resource_type=l.resource_type,
            resource_id=l.resource_id,
            details=l.details,
            created_at=l.created_at.isoformat() if hasattr(l, "created_at") and l.created_at else ""
        )
        for l in logs
    ]
