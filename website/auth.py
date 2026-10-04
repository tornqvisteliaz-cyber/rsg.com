"""Separate public customer authentication from privileged admin access."""

import secrets
from datetime import datetime
from urllib.parse import urlparse

import pyotp
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from user_agents import parse

from . import db
from .email import (
    send_delete_account_email,
    send_email_verification,
    send_welcome_email,
)
from .extensions import bcrypt, limiter
from .models import AdminSession, AdminUser, AuditLog, Customer

auth = Blueprint("auth", __name__)


def safe_next_url(default_endpoint="views.home"):
    target = request.values.get("next", "").strip()
    if target:
        parsed = urlparse(target)
        if not parsed.netloc and target.startswith("/"):
            return target
    return url_for(default_endpoint)


def get_current_customer():
    customer_id = session.get("customer_id")
    if not customer_id:
        return None
    customer = db.session.get(Customer, customer_id)
    if not customer or customer.banned:
        session.clear()
        return None
    return customer


def start_customer_session(customer):
    session.clear()
    session.permanent = True
    session["customer_id"] = customer.id
    session["customer_name"] = customer.name
    session["customer_email"] = customer.email
    customer.last_login = datetime.utcnow()
    db.session.commit()


def create_audit(action, target=None):
    db.session.add(AuditLog(
        admin_id=session.get("admin_id"),
        action=action,
        target=target,
        ip_address=request.remote_addr,
    ))


def create_device_session(admin):
    parsed = parse(request.headers.get("User-Agent", ""))
    country = request.headers.get("CF-IPCountry") or request.headers.get("X-Country")
    previous_ips = {
        item.ip_address
        for item in AdminSession.query.filter_by(admin_id=admin.id).all()
    }
    unusual = bool(previous_ips and request.remote_addr not in previous_ips)
    device = AdminSession(
        admin_id=admin.id,
        ip_address=request.remote_addr,
        country=country,
        browser=parsed.browser.family,
        operating_system=parsed.os.family,
        device_type=parsed.device.family,
        unusual=unusual,
    )
    db.session.add(device)
    return device


def start_admin_session(admin):
    device = create_device_session(admin)
    db.session.flush()
    session.clear()
    session.update({
        "logged_in": True,
        "admin_id": admin.id,
        "username": admin.username,
        "role": admin.role,
        "device_session_id": device.id,
    })
    admin.last_login = datetime.utcnow()
    create_audit("Admin logged in", admin.username)
    db.session.commit()


@auth.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        admin = AdminUser.query.filter_by(username=username).first()
        if not admin or not admin.enabled or not bcrypt.check_password_hash(admin.password_hash, password):
            flash("Invalid username or password.", "danger")
            return redirect(url_for("auth.admin_login"))
        if admin.two_factor_enabled:
            session.clear()
            session["pending_admin_id"] = admin.id
            return redirect(url_for("auth.verify_two_factor"))
        start_admin_session(admin)
        return redirect(url_for("admin.dashboard"))
    return render_template("admin/login.html")


@auth.route("/admin/verify-2fa", methods=["GET", "POST"])
@limiter.limit("8 per minute", methods=["POST"])
def verify_two_factor():
    admin_id = session.get("pending_admin_id")
    admin = db.session.get(AdminUser, admin_id) if admin_id else None
    if not admin:
        return redirect(url_for("auth.admin_login"))
    if request.method == "POST":
        code = request.form.get("code", "").replace(" ", "")
        if admin.two_factor_secret and pyotp.TOTP(admin.two_factor_secret).verify(code, valid_window=1):
            start_admin_session(admin)
            return redirect(url_for("admin.dashboard"))
        flash("Invalid authentication code.", "danger")
    return render_template("admin/verify_2fa.html")


@auth.route("/admin/logout", methods=["POST"])
def logout():
    device_id = session.get("device_session_id")
    device = db.session.get(AdminSession, device_id) if device_id else None
    if device:
        device.active = False
        create_audit("Admin logged out", session.get("username"))
        db.session.commit()
    session.clear()
    return redirect(url_for("views.home"))


@auth.route("/signup", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def signup():
    if get_current_customer():
        return redirect(url_for("auth.account"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or len(password) < 8:
            flash("Enter a name, email, and a password of at least 8 characters.", "danger")
        elif Customer.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "danger")
        else:
            customer = Customer(
                name=name,
                email=email,
                password_hash=bcrypt.generate_password_hash(password).decode("utf-8"),
            )
            db.session.add(customer)
            db.session.commit()
            send_welcome_email(email, name)
            start_customer_session(customer)
            flash("Your account has been created.", "success")
            return redirect(safe_next_url("auth.account"))

    return render_template("signup.html")


@auth.route("/login", methods=["GET", "POST"], endpoint="login")
@limiter.limit("5 per minute", methods=["POST"])
def customer_login():
    if get_current_customer():
        return redirect(safe_next_url("auth.account"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        customer = Customer.query.filter_by(email=email).first()
        valid = (
            customer
            and not customer.banned
            and customer.password_hash
            and bcrypt.check_password_hash(customer.password_hash, password)
        )

        if not valid:
            flash("Invalid email or password.", "danger")
            return redirect(url_for("auth.login", next=request.form.get("next", "")))

        start_customer_session(customer)
        return redirect(safe_next_url("auth.account"))

    return render_template("login.html", next=request.args.get("next", ""))


@auth.route("/logout", methods=["POST"])
def customer_logout():
    session.clear()
    return redirect(url_for("views.home"))


@auth.route("/account")
def account():
    customer = get_current_customer()
    if not customer:
        flash("Please log in first.", "warning")
        return redirect(url_for("auth.login", next=url_for("auth.account")))
    return render_template("account.html", customer=customer, orders=customer.orders)


@auth.route("/account/settings", methods=["GET", "POST"])
def account_settings():
    customer = get_current_customer()
    if not customer:
        flash("Please log in first.", "warning")
        return redirect(url_for("auth.login", next=url_for("auth.account_settings")))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")

        if not name or not email:
            flash("Name and email are required.", "danger")
            return redirect(url_for("auth.account_settings"))

        customer.name = name
        session["customer_name"] = name

        if email != customer.email:
            if Customer.query.filter_by(email=email).first():
                flash("That email is already in use.", "danger")
                return redirect(url_for("auth.account_settings"))

            token = secrets.token_urlsafe(32)
            customer.pending_email = email
            customer.email_token = token
            db.session.commit()

            verify_url = url_for("auth.verify_email", token=token, _external=True)
            send_email_verification(email, customer.name, verify_url)
            flash("Check your new email and click the verification link to confirm the change.", "info")
            return redirect(url_for("auth.account_settings"))

        if new_password:
            if not bcrypt.check_password_hash(customer.password_hash, current_password):
                flash("Current password is incorrect.", "danger")
                return redirect(url_for("auth.account_settings"))
            if len(new_password) < 8:
                flash("New password must be at least 8 characters.", "danger")
                return redirect(url_for("auth.account_settings"))
            customer.password_hash = bcrypt.generate_password_hash(new_password).decode("utf-8")

        db.session.commit()
        flash("Account updated successfully.", "success")
        return redirect(url_for("auth.account"))

    return render_template("account_settings.html", customer=customer)


@auth.route("/verify-email/<token>")
def verify_email(token):
    customer = Customer.query.filter_by(email_token=token).first()
    if not customer or not customer.pending_email:
        flash("Invalid or expired verification link.", "danger")
        return redirect(url_for("auth.login"))

    if Customer.query.filter_by(email=customer.pending_email).first():
        flash("That email is already in use.", "danger")
        customer.pending_email = None
        customer.email_token = None
        db.session.commit()
        return redirect(url_for("auth.account_settings"))

    customer.email = customer.pending_email
    customer.pending_email = None
    customer.email_token = None
    db.session.commit()

    if session.get("customer_id") == customer.id:
        session["customer_email"] = customer.email
        flash("Your email has been verified and updated.", "success")
        return redirect(url_for("auth.account"))

    flash("Your email has been verified. Please log in.", "success")
    return redirect(url_for("auth.login"))


@auth.route("/account/delete", methods=["POST"])
def request_delete_account():
    customer = get_current_customer()
    if not customer:
        flash("Please log in first.", "warning")
        return redirect(url_for("auth.login"))

    token = secrets.token_urlsafe(32)
    customer.email_token = token
    customer.pending_email = None
    db.session.commit()

    delete_url = url_for("auth.confirm_delete_account", token=token, _external=True)
    send_delete_account_email(customer.email, customer.name, delete_url)
    flash("We sent a confirmation link to your email. Click it to permanently delete your account.", "info")
    return redirect(url_for("auth.account_settings"))


@auth.route("/account/delete/<token>")
def confirm_delete_account(token):
    customer = Customer.query.filter_by(email_token=token).first()
    if not customer:
        flash("Invalid or expired deletion link.", "danger")
        return redirect(url_for("auth.login"))

    customer_id = customer.id
    db.session.delete(customer)
    db.session.commit()

    if session.get("customer_id") == customer_id:
        session.clear()

    flash("Your account has been permanently deleted.", "success")
    return redirect(url_for("views.home"))