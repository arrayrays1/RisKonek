from passlib.context import CryptContext
from fastapi import Request
from fastapi.responses import RedirectResponse

# Password hashing setup
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(plain_password: str) -> str:
    """Convert plain text password to hashed version for storage"""
    return pwd_context.hash(plain_password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check if a plain password matches the stored hash"""
    return pwd_context.verify(plain_password, hashed_password)

def get_current_user(request: Request):
    """Get the logged-in user from the session cookie"""
    return request.session.get("user")

def require_login(request: Request):
    """
    Call this at the top of any protected route.
    Returns the user dict if logged in, or redirects to login if not.
    """
    user = request.session.get("user")
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return user

def require_role(request: Request, allowed_roles: list):
    """
    Call this to restrict a route to specific roles.
    Example: require_role(request, ["admin"])
    """
    user = request.session.get("user")
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user["role"] not in allowed_roles:
        return RedirectResponse(url="/unauthorized", status_code=302)
    return user