"""
Enterprise Identity and Email Policy Validators.

Enforces corporate domain restrictions by blocking free/consumer email domains
and disposable temporary email services during tenant registration and user provisioning.
"""

import re
from typing import Set
from fastapi import HTTPException, status
from core.config import settings

# Common free consumer email domains that are prohibited for enterprise accounts
DISALLOWED_CONSUMER_DOMAINS: Set[str] = {
    # Google
    "gmail.com", "googlemail.com",
    # Yahoo
    "yahoo.com", "yahoo.co.uk", "yahoo.fr", "yahoo.es", "yahoo.de",
    "yahoo.ca", "yahoo.com.br", "yahoo.in", "ymail.com", "rocketmail.com",
    # Microsoft
    "hotmail.com", "hotmail.co.uk", "hotmail.fr", "hotmail.es", "hotmail.de",
    "outlook.com", "outlook.co.uk", "live.com", "live.co.uk", "msn.com", "passport.com",
    # Apple
    "icloud.com", "me.com", "mac.com",
    # Other consumer webmail
    "aol.com", "aim.com",
    "protonmail.com", "proton.me", "pm.me",
    "zoho.com",
    "mail.com", "email.com",
    "gmx.com", "gmx.net", "gmx.de",
    "yandex.com", "yandex.ru", "yandex.net",
    "tutanota.com", "tutamail.com", "tuta.io",
    "fastmail.com", "hushmail.com",
    "lycos.com", "inbox.com", "rediffmail.com",

    # Disposable / temporary / burner email services
    "10minutemail.com", "tempmail.com", "guerrillamail.com", "mailinator.com",
    "throwawaymail.com", "sharklasers.com", "getairmail.com", "dispostable.com",
    "trashmail.com", "yopmail.com", "fakemailgenerator.com", "nada.ltd",
    "temp-mail.org", "burnermail.io", "mohmal.com"
}

EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
)


def extract_email_domain(email: str) -> str:
    """Extracts and normalizes the domain from an email address."""
    if not email or "@" not in email:
        return ""
    parts = email.strip().rsplit("@", 1)
    return parts[1].lower().strip()


def is_consumer_or_disposable_domain(domain: str) -> bool:
    """Checks if a domain or its parent domain belongs to the disallowed list."""
    domain_clean = domain.lower().strip()
    if domain_clean in DISALLOWED_CONSUMER_DOMAINS:
        return True
    domain_parts = domain_clean.split(".")
    for i in range(len(domain_parts) - 1):
        sub = ".".join(domain_parts[i:])
        if sub in DISALLOWED_CONSUMER_DOMAINS:
            return True
    return False


def validate_enterprise_email(email: str, allow_public_domains: bool = False) -> str:
    """
    Validates that the given email is syntactically valid and belongs to an
    enterprise/corporate domain (rejecting public consumer & disposable domains).

    Returns the normalized (lowercased, stripped) email if valid.
    Raises HTTPException(400) if invalid or prohibited.
    """
    if not email or not isinstance(email, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email address is required."
        )

    cleaned_email = email.strip().lower()

    if not EMAIL_REGEX.match(cleaned_email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid email format: '{email}'."
        )

    # Check if enterprise domain enforcement is disabled via config
    enforce_enterprise = getattr(settings, "ENFORCE_ENTERPRISE_EMAIL_DOMAINS", True)
    if not enforce_enterprise or allow_public_domains:
        return cleaned_email

    domain = extract_email_domain(cleaned_email)

    if is_consumer_or_disposable_domain(domain):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Enterprise registration prohibited: '{domain}' is a public consumer/personal email provider. "
                "Enterprise access requires an official organization or corporate business email address."
            )
        )

    return cleaned_email
