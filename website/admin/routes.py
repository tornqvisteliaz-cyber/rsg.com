import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlsplit

import pyotp
from flask import current_app, flash, redirect, render_template, request, send_from_directory, session, url_for
from sqlalchemy import func, or_

from . import admin
from .decorators import admin_required
from .. import db
from ..extensions import bcrypt
from ..models import AdminSession, AdminUser, AuditLog, CatalogProduct, Customer, Order, Refund, SupportTicket


def audit(action, target=None):
    db.session.add(AuditLog(admin_id=session.get("admin_id"), action=action, target=target, ip_address=request.remote_addr))


@admin.route("/")
@admin_required()
def dashboard():
    paid_revenue = db.session.query(func.coalesce(func.sum(Order.amount), 0)).filter(Order.payment_status == "Paid").scalar()
    recent_sessions = AdminSession.query.order_by(AdminSession.created_at.desc()).limit(8).all()
    recent_orders = Order.query.order_by(Order.created_at.desc()).limit(8).all()
    recent_logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(10).all()
    return render_template("admin/dashboard.html",
        revenue=paid_revenue, total_admins=AdminUser.query.count(), total_customers=Customer.query.count(),
        total_products=CatalogProduct.query.count(), draft_products=CatalogProduct.query.filter_by(status="draft").count(),
        active_users=AdminSession.query.filter_by(active=True).count(), total_orders=Order.query.count(),
        pending_orders=Order.query.filter_by(status="Pending").count(), recent_sessions=recent_sessions,
        recent_orders=recent_orders, recent_logs=recent_logs,
    )


PRODUCT_STATUSES = ("draft", "in_development", "coming_soon", "published", "archived")
PRODUCT_CURRENCIES = ("USD", "SEK", "EUR", "GBP")


def _valid_product_url(value):
    if not value:
        return True
    if len(value) > 500 or any(ord(char) < 32 for char in value) or "\\" in value:
        return False
    parsed = urlsplit(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _price_label(minor_units, currency):
    whole, cents = divmod(int(minor_units or 0), 100)
    amount = f"{whole}.{cents:02d}"
    symbols = {"USD": "$", "EUR": "€", "GBP": "£"}
    return f"{symbols[currency]}{amount}" if currency in symbols else f"{amount} {currency}"


def _save_product_form(product=None):
    data = request.form
    name = data.get("name", "").strip()
    product_id = product.id if product else data.get("id", "").strip().lower()
    if not product_id and name:
        product_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    status = data.get("status", "draft")
    currency = data.get("currency", "USD").upper()
    try:
        price = Decimal(data.get("price_amount", "").strip())
        if not price.is_finite() or price < 0 or price > Decimal("99999999.99"):
            raise InvalidOperation
        price_minor_units = int((price * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        flash("Enter a valid price from 0.00 to 99,999,999.99.", "danger")
        return False

    if not name or len(name) > 200:
        flash("Product name is required and must be 200 characters or fewer.", "danger")
        return False
    if not product_id or len(product_id) > 80 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", product_id):
        flash("Use a product ID made of lowercase letters, numbers, and single hyphens.", "danger")
        return False
    existing = db.session.get(CatalogProduct, product_id)
    if existing is not None and existing is not product:
        flash("That product ID is already in use.", "danger")
        return False
    if currency not in PRODUCT_CURRENCIES:
        flash("Choose a supported currency.", "danger")
        return False
    if status not in PRODUCT_STATUSES:
        flash("Choose a valid product status.", "danger")
        return False

    fields = {
        "simulator": (data.get("simulator", "").strip() or "MSFS 2024", 80),
        "version": (data.get("version", "").strip() or "1.0.0", 40),
        "folder_name": (data.get("folder_name", "").strip() or f"rsg-{product_id}", 200),
        "image_url": (data.get("image_url", "").strip(), 500),
        "download_url": (data.get("download_url", "").strip(), 500),
        "buy_url": (data.get("buy_url", "").strip(), 500),
    }
    for field, (value, limit) in fields.items():
        if len(value) > limit:
            flash(f"{field.replace('_', ' ').capitalize()} must be {limit} characters or fewer.", "danger")
            return False
    if not all(_valid_product_url(fields[field][0]) for field in ("image_url", "download_url", "buy_url")):
        flash("Image, download, and purchase links must use an http:// or https:// URL.", "danger")
        return False

    if product is None:
        product = CatalogProduct(id=product_id, name=name)
        db.session.add(product)
    product.name = name
    product.simulator = fields["simulator"][0]
    product.version = fields["version"][0]
    product.folder_name = fields["folder_name"][0]
    product.image_url = fields["image_url"][0]
    product.download_url = fields["download_url"][0]
    product.buy_url = fields["buy_url"][0]
    product.description = data.get("description", "").strip()
    product.status = status
    product.price_minor_units = price_minor_units
    product.currency = currency
    product.price = _price_label(price_minor_units, currency)
    audit(f"{session['username']} saved product {product.name}", product.id)
    db.session.commit()
    return product


def _render_product_form(product=None):
    if request.method == "POST":
        saved = _save_product_form(product)
        if saved:
            flash("Product saved. Payment checkout is not enabled yet.", "success")
            return redirect(url_for("admin.products"))
    return render_template(
        "admin/product_form.html",
        product=product,
        form_data=request.form if request.method == "POST" else {},
        statuses=PRODUCT_STATUSES,
        currencies=PRODUCT_CURRENCIES,
    )


@admin.route("/products")
@admin_required("Support")
def products():
    search = request.args.get("search", "").strip()
    status = request.args.get("status", "")
    query = CatalogProduct.query
    if search:
        query = query.filter(or_(CatalogProduct.name.contains(search), CatalogProduct.id.contains(search)))
    if status in PRODUCT_STATUSES:
        query = query.filter(CatalogProduct.status == status)
    items = query.order_by(CatalogProduct.name.asc()).all()
    return render_template(
        "admin/products.html",
        products=items,
        search=search,
        status=status,
        statuses=PRODUCT_STATUSES,
    )


@admin.route("/products/new", methods=["GET", "POST"])
@admin_required("Admin")
def product_new():
    return _render_product_form()


@admin.route("/products/<string:product_id>/edit", methods=["GET", "POST"])
@admin_required("Admin")
def product_edit(product_id):
    product = db.get_or_404(CatalogProduct, product_id)
    return _render_product_form(product)


@admin.route("/products/<string:product_id>/archive", methods=["POST"])
@admin_required("Admin")
def product_archive(product_id):
    product = db.get_or_404(CatalogProduct, product_id)
    product.status = "draft" if product.status == "archived" else "archived"
    audit(f"{session['username']} {'restored' if product.status == 'draft' else 'archived'} product {product.name}", product.id)
    db.session.commit()
    flash("Product status updated.", "success")
    return redirect(url_for("admin.products"))


@admin.route("/admins")
@admin_required("Owner")
def admins():
    return render_template("admin/admins.html", users=AdminUser.query.order_by(AdminUser.username).all())


@admin.route("/admins/create", methods=["GET", "POST"])
@admin_required("Owner")
def create_admin():
    if request.method == "POST":
        username, email, password = (request.form.get(key, "").strip() for key in ("username", "email", "password"))
        role = request.form.get("role", "Support")
        if not username or not email or len(password) < 12 or role not in ("Owner", "Admin", "Support"):
            flash("Provide a unique username/email, a 12-character password, and a valid role.", "danger")
        elif AdminUser.query.filter(or_(AdminUser.username == username, AdminUser.email == email)).first():
            flash("Username or email already exists.", "danger")
        else:
            user = AdminUser(username=username, email=email, role=role, enabled=True,
                password_hash=bcrypt.generate_password_hash(password).decode("utf-8"))
            db.session.add(user)
            audit(f"{session['username']} created {role} {username}", username)
            db.session.commit()
            return redirect(url_for("admin.admins"))
    return render_template("admin/create_admin.html")


@admin.route("/admins/<int:id>/edit", methods=["GET", "POST"])
@admin_required("Owner")
def edit_admin(id):
    user = db.get_or_404(AdminUser, id)
    if request.method == "POST":
        role = request.form.get("role", user.role)
        email = request.form.get("email", "").strip()
        if role not in ("Owner", "Admin", "Support") or not email:
            flash("Invalid admin details.", "danger")
        elif AdminUser.query.filter(AdminUser.email == email, AdminUser.id != id).first():
            flash("That email is already in use.", "danger")
        else:
            user.email, user.role = email, role
            audit(f"{session['username']} edited admin {user.username}", user.username)
            db.session.commit()
            return redirect(url_for("admin.admins"))
    return render_template("admin/edit_admin.html", user=user)


@admin.route("/admins/<int:id>/toggle", methods=["POST"])
@admin_required("Owner")
def toggle_admin(id):
    user = db.get_or_404(AdminUser, id)
    if user.id == session["admin_id"]:
        flash("You cannot disable your own account.", "danger")
    else:
        user.enabled = not user.enabled
        if not user.enabled:
            AdminSession.query.filter_by(admin_id=user.id, active=True).update({"active": False})
        audit(f"{session['username']} {'enabled' if user.enabled else 'disabled'} admin {user.username}", user.username)
        db.session.commit()
    return redirect(url_for("admin.admins"))


@admin.route("/admins/<int:id>/delete", methods=["POST"])
@admin_required("Owner")
def delete_admin(id):
    user = db.get_or_404(AdminUser, id)
    if user.id == session["admin_id"] or AdminUser.query.filter_by(role="Owner", enabled=True).count() <= 1 and user.role == "Owner":
        flash("You cannot delete the last active Owner or yourself.", "danger")
    else:
        audit(f"{session['username']} deleted admin {user.username}", user.username)
        db.session.delete(user)
        db.session.commit()
    return redirect(url_for("admin.admins"))


@admin.route("/admins/<int:id>/password", methods=["POST"])
@admin_required("Owner")
def reset_admin_password(id):
    user = db.get_or_404(AdminUser, id)
    password = request.form.get("password", "")
    if len(password) < 12:
        flash("Admin passwords must be at least 12 characters.", "danger")
    else:
        user.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")
        AdminSession.query.filter_by(admin_id=user.id, active=True).update({"active": False})
        audit(f"{session['username']} reset password for {user.username}", user.username)
        db.session.commit()
        flash("Password reset and existing sessions ended.", "success")
    return redirect(url_for("admin.edit_admin", id=id))


@admin.route("/two-factor", methods=["GET", "POST"])
@admin_required()
def two_factor():
    user = db.get_or_404(AdminUser, session["admin_id"])
    if request.method == "POST":
        code = request.form.get("code", "")
        if user.two_factor_secret and pyotp.TOTP(user.two_factor_secret).verify(code, valid_window=1):
            user.two_factor_enabled = True
            audit(f"{user.username} enabled two-factor authentication", user.username)
            db.session.commit()
            flash("Two-factor authentication is enabled.", "success")
            return redirect(url_for("admin.dashboard"))
        flash("Invalid verification code.", "danger")
    if not user.two_factor_secret:
        user.two_factor_secret = pyotp.random_base32()
        db.session.commit()
    return render_template("admin/two_factor.html", secret=user.two_factor_secret,
        uri=pyotp.TOTP(user.two_factor_secret).provisioning_uri(name=user.email, issuer_name="RSG Software"))


@admin.route("/orders")
@admin_required()
def orders():
    search, status, payment = request.args.get("search", "").strip(), request.args.get("status", ""), request.args.get("payment", "")
    query = Order.query.join(Customer)
    if search:
        query = query.filter(or_(Customer.email.contains(search), Customer.name.contains(search), Order.id.cast(db.String).contains(search)))
    if status:
        query = query.filter(Order.status == status)
    if payment:
        query = query.filter(Order.payment_status == payment)
    return render_template("admin/orders.html", orders=query.order_by(Order.created_at.desc()).all(), search=search, status=status, payment=payment)


@admin.route("/orders/<int:id>/status", methods=["POST"])
@admin_required("Support")
def update_order_status(id):
    order = db.get_or_404(Order, id)
    status = request.form.get("status")
    if status in ("Pending", "Processing", "Completed", "Cancelled", "Refunded"):
        order.status = status
        audit(f"{session['role']} {session['username']} updated Order #{id} to {status}", f"Order #{id}")
        db.session.commit()
    return redirect(url_for("admin.orders"))


@admin.route("/orders/<int:id>/refund", methods=["POST"])
@admin_required("Admin")
def refund_order(id):
    order = db.get_or_404(Order, id)
    if order.payment_status == "Refunded":
        flash("This order has already been refunded.", "warning")
    else:
        db.session.add(Refund(order_id=order.id, amount=order.amount, reason=request.form.get("reason", "Admin refund")))
        order.status, order.payment_status = "Refunded", "Refunded"
        audit(f"{session['role']} {session['username']} refunded Order #{id}", f"Order #{id}")
        db.session.commit()
    return redirect(url_for("admin.orders"))


@admin.route("/orders/<int:id>/invoice")
@admin_required()
def invoice(id):
    order = db.get_or_404(Order, id)
    if not order.invoice_file:
        flash("No invoice has been uploaded for this order.", "warning")
        return redirect(url_for("admin.orders"))
    return send_from_directory(os.path.join(current_app.root_path, "static", "invoices"), os.path.basename(order.invoice_file), as_attachment=True)


@admin.route("/customers")
@admin_required()
def customers():
    search = request.args.get("search", "").strip()
    query = Customer.query
    if search:
        query = query.filter(or_(Customer.name.contains(search), Customer.email.contains(search)))
    return render_template("admin/customers.html", customers=query.order_by(Customer.created_at.desc()).all(), search=search)


@admin.route("/customers/<int:id>")
@admin_required()
def customer_detail(id):
    customer = db.get_or_404(Customer, id)
    return render_template("admin/customer_detail.html", customer=customer,
        orders=Order.query.filter_by(customer_id=id).order_by(Order.created_at.desc()).all(),
        tickets=SupportTicket.query.filter_by(customer_id=id).order_by(SupportTicket.created_at.desc()).all())


@admin.route("/customers/<int:id>/ban", methods=["POST"])
@admin_required("Support")
def ban_customer(id):
    customer = db.get_or_404(Customer, id)
    customer.banned = not customer.banned
    audit(f"{session['username']} {'banned' if customer.banned else 'unbanned'} customer {customer.email}", customer.email)
    db.session.commit()
    return redirect(url_for("admin.customer_detail", id=id))


@admin.route("/customers/<int:id>/password", methods=["POST"])
@admin_required("Support")
def reset_customer_password(id):
    customer = db.get_or_404(Customer, id)
    password = request.form.get("password", "")
    if len(password) < 8:
        flash("Customer passwords must be at least 8 characters.", "danger")
    else:
        customer.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")
        audit(f"{session['username']} reset password for customer {customer.email}", customer.email)
        db.session.commit()
        flash("Customer password reset.", "success")
    return redirect(url_for("admin.customer_detail", id=id))


@admin.route("/devices")
@admin_required()
def devices():
    return render_template("admin/devices.html", sessions=AdminSession.query.order_by(AdminSession.last_active.desc()).all())


@admin.route("/devices/<int:id>/logout", methods=["POST"])
@admin_required("Admin")
def logout_device(id):
    device = db.get_or_404(AdminSession, id)
    device.active = False
    audit(f"{session['username']} ended a device session", str(id))
    db.session.commit()
    return redirect(url_for("admin.devices"))


@admin.route("/admins/<int:id>/logout-all", methods=["POST"])
@admin_required("Admin")
def logout_all_devices(id):
    user = db.get_or_404(AdminUser, id)
    AdminSession.query.filter_by(admin_id=user.id, active=True).update({"active": False})
    audit(f"{session['username']} ended all sessions for {user.username}", user.username)
    db.session.commit()
    return redirect(url_for("admin.devices"))


@admin.route("/logs")
@admin_required("Admin")
def logs():
    return render_template("admin/audit_logs.html", logs=AuditLog.query.order_by(AuditLog.created_at.desc()).limit(500).all())
from ..models import NewsletterPost   # add this import at the top with the others
from werkzeug.utils import secure_filename
import os
from datetime import datetime

UPLOAD_FOLDER = "static/uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@admin.route("/newsletter")
@admin_required()
def newsletter_list():
    posts = NewsletterPost.query.order_by(NewsletterPost.date_posted.desc()).all()
    return render_template("admin/newsletter_list.html", posts=posts)


@admin.route("/newsletter/new", methods=["GET", "POST"])
@admin_required()
def newsletter_new():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        author = session.get("username", "Admin")

        if not title or not content:
            flash("Title and content are required.", "danger")
            return redirect(url_for("admin.newsletter_new"))

        image_filename = None
        if "image" in request.files:
            file = request.files["image"]
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                image_filename = f"{timestamp}_{filename}"
                upload_path = os.path.join(current_app.root_path, UPLOAD_FOLDER)
                os.makedirs(upload_path, exist_ok=True)
                file.save(os.path.join(upload_path, image_filename))

        post = NewsletterPost(
            title=title,
            content=content,
            author=author,
            image_filename=image_filename
        )
        db.session.add(post)
        audit(f"{session['username']} created newsletter post", title)
        db.session.commit()
        flash("Newsletter published!", "success")
        return redirect(url_for("admin.newsletter_list"))

    return render_template("admin/newsletter_form.html")


@admin.route("/newsletter/<int:id>/delete", methods=["POST"])
@admin_required()
def newsletter_delete(id):
    post = db.get_or_404(NewsletterPost, id)

    if post.image_filename:
        path = os.path.join(current_app.root_path, UPLOAD_FOLDER, post.image_filename)
        if os.path.exists(path):
            os.remove(path)

    audit(f"{session['username']} deleted newsletter post", post.title)
    db.session.delete(post)
    db.session.commit()
    flash("Post deleted.", "success")
    return redirect(url_for("admin.newsletter_list"))