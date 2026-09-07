from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User

SESSION_USER_KEY = "user_id"


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    user_id = request.session.get(SESSION_USER_KEY)
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def require_can_create(user: User = Depends(require_user)) -> User:
    if not user.can_create:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted to create proposals")
    return user


def require_can_approve(user: User = Depends(require_user)) -> User:
    if not user.can_approve:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted to approve proposals")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user
