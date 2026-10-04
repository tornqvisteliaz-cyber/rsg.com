from datetime import datetime
from functools import wraps

from flask import flash, redirect, session, url_for

from .. import db
from ..models import AdminSession

ROLE_LEVELS = {"Support": 1, "Admin": 2, "Owner": 3}


def admin_required(role=None):
    """Require an active, non-revoked admin device session and role level."""
    def decorator(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            if not session.get("logged_in") or not session.get("admin_id"):
                return redirect(url_for("auth.admin_login"))
            device = db.session.get(AdminSession, session.get("device_session_id"))
            if not device or not device.active or device.admin_id != session["admin_id"]:
                session.clear()
                flash("Your session has ended.", "warning")
                return redirect(url_for("auth.admin_login"))
            device.last_active = datetime.utcnow()
            db.session.commit()
            if role and ROLE_LEVELS.get(session.get("role"), 0) < ROLE_LEVELS[role]:
                flash("Permission denied.", "danger")
                return redirect(url_for("admin.dashboard"))
            return function(*args, **kwargs)
        return wrapper
    return decorator
