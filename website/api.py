import os
import re
import secrets

from flask import Blueprint, jsonify, request
from functools import wraps

from . import db
from .extensions import bcrypt, limiter
from .models import AdminUser, CatalogLivery, CatalogProduct, Customer, Order

api = Blueprint("api", __name__, url_prefix="/api")

DEFAULT_IMAGE = "https://i.postimg.cc/28WybSM2/metroliner.jpg"
DEFAULT_BUY = "https://rsg-website.onrender.com/aircraft/seabee"


def slugify(value):
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or secrets.token_hex(4)


def seed_catalog():
    if CatalogProduct.query.count() == 0:
        db.session.add(CatalogProduct(
            id="seabee",
            name="Republic RC-3 Seabee",
            simulator="MSFS 2024",
            version="0.1.0-dev",
            folder_name="rsg-seabee",
            download_url=os.getenv("SEABEE_DOWNLOAD_URL", ""),
            image_url=DEFAULT_IMAGE,
            price="$29.99",
            description="Amphibious flying boat for Microsoft Flight Simulator 2024.",
            buy_url=DEFAULT_BUY,
        ))
        db.session.commit()


def bearer_token():
    return request.headers.get("Authorization", "").replace("Bearer ", "").strip()


def get_account_from_token():
    token = bearer_token()
    if not token:
        return None, None
    admin = AdminUser.query.filter_by(api_token=token).first()
    if admin and admin.enabled:
        return admin, "admin"
    customer = Customer.query.filter_by(api_token=token).first()
    if customer and not customer.banned:
        return customer, "customer"
    return None, None


def require_account(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        seed_catalog()
        account, kind = get_account_from_token()
        if not account:
            return jsonify({"error": "Unauthorized"}), 401
        return fn(account, kind, *args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        seed_catalog()
        account, kind = get_account_from_token()
        if kind != "admin":
            return jsonify({"error": "Admin only. Log in with the admin account."}), 403
        return fn(account, *args, **kwargs)
    return wrapper


@api.route("/login", methods=["POST"])
@limiter.limit("8 per minute")
def api_login():
    data = request.get_json(silent=True) or {}
    raw_login = (data.get("email") or "").strip()
    email = raw_login.lower()
    password = data.get("password") or ""

    admin = AdminUser.query.filter(
        (AdminUser.email == email) | (AdminUser.username == raw_login)
    ).first()
    if admin and admin.enabled and bcrypt.check_password_hash(admin.password_hash, password):
        if not admin.api_token:
            admin.api_token = secrets.token_urlsafe(32)
            db.session.commit()
        return jsonify({
            "token": admin.api_token,
            "name": admin.username,
            "email": admin.email,
            "role": admin.role,
            "is_admin": True,
        })

    customer = Customer.query.filter_by(email=email).first()
    if (
        not customer
        or customer.banned
        or not customer.password_hash
        or not bcrypt.check_password_hash(customer.password_hash, password)
    ):
        return jsonify({"error": "Invalid email or password"}), 401

    if not customer.api_token:
        customer.api_token = secrets.token_urlsafe(32)
        db.session.commit()

    return jsonify({
        "token": customer.api_token,
        "name": customer.name,
        "email": customer.email,
        "role": "Customer",
        "is_admin": False,
    })


def owns_content(account, kind):
    if kind == "admin" or os.getenv("RSG_DEV_UNLOCK", "").lower() == "true":
        return True
    return Order.query.filter_by(customer_id=account.id, payment_status="Paid").count() > 0


def product_dict(item, owned):
    return {
        "id": item.id,
        "name": item.name,
        "simulator": item.simulator,
        "version": item.version,
        "folder_name": item.folder_name,
        "download_url": item.download_url if owned else "",
        "status": item.status,
        "owned": owned,
        "image_url": item.image_url or DEFAULT_IMAGE,
        "price": item.price or "$29.99",
        "description": item.description or "",
        "buy_url": item.buy_url or DEFAULT_BUY,
    }


def livery_dict(item, owned):
    return {
        "id": item.id,
        "name": item.name,
        "aircraft": item.aircraft,
        "folder_name": item.folder_name,
        "download_url": item.download_url if owned else "",
        "owned": owned,
        "image_url": item.image_url or DEFAULT_IMAGE,
        "price": "",
        "description": "",
        "buy_url": "",
        "version": "",
        "simulator": "",
    }


@api.route("/products")
@require_account
def api_products(account, kind):
    owned = owns_content(account, kind)
    items = [product_dict(item, owned) for item in CatalogProduct.query.order_by(CatalogProduct.name).all()]
    return jsonify({"products": items})


@api.route("/liveries")
@require_account
def api_liveries(account, kind):
    owned = owns_content(account, kind)
    items = [livery_dict(item, owned) for item in CatalogLivery.query.order_by(CatalogLivery.name).all()]
    return jsonify({"liveries": items})


@api.route("/admin/overview")
@require_admin
def api_admin_overview(admin):
    return jsonify({
        "name": admin.username,
        "role": admin.role,
        "products": CatalogProduct.query.count(),
        "liveries": CatalogLivery.query.count(),
        "customers": Customer.query.count(),
    })


@api.route("/admin/products", methods=["POST"])
@require_admin
def api_add_product(admin):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    item_id = slugify(data.get("id") or name)
    item = db.session.get(CatalogProduct, item_id)
    if item is None:
        item = CatalogProduct(id=item_id, name=name)
        db.session.add(item)
    item.name = name
    item.simulator = data.get("simulator") or item.simulator or "MSFS 2024"
    item.version = data.get("version") or item.version or "0.1.0"
    item.folder_name = data.get("folder_name") or item.folder_name or f"rsg-{item_id}"
    item.download_url = data.get("download_url") if data.get("download_url") is not None else (item.download_url or "")
    item.image_url = data.get("image_url") or item.image_url or DEFAULT_IMAGE
    item.price = data.get("price") or item.price or "$29.99"
    item.description = data.get("description") if data.get("description") is not None else (item.description or "")
    item.buy_url = data.get("buy_url") or item.buy_url or DEFAULT_BUY
    item.status = data.get("status") or item.status or "in_development"
    db.session.commit()
    return jsonify({"ok": True, "product": product_dict(item, True)})


@api.route("/admin/liveries", methods=["POST"])
@require_admin
def api_add_livery(admin):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    item_id = slugify(data.get("id") or name)
    item = db.session.get(CatalogLivery, item_id)
    if item is None:
        item = CatalogLivery(id=item_id, name=name)
        db.session.add(item)
    item.name = name
    item.aircraft = data.get("aircraft") or item.aircraft or "Republic RC-3 Seabee"
    item.folder_name = data.get("folder_name") or item.folder_name or f"rsg-{item_id}"
    item.download_url = data.get("download_url") if data.get("download_url") is not None else (item.download_url or "")
    item.image_url = data.get("image_url") or item.image_url or DEFAULT_IMAGE
    db.session.commit()
    return jsonify({"ok": True, "livery": livery_dict(item, True)})