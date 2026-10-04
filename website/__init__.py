# website/__init__.py

import os

from flask import Flask
from sqlalchemy import inspect, text
from flask_sqlalchemy import SQLAlchemy

from .extensions import (
    bcrypt,
    login_manager,
    csrf,
    limiter
)

db = SQLAlchemy()


def upgrade_database_schema():
    inspector = inspect(db.engine)

    if "admin_user" not in inspector.get_table_names():
        return

    admin_columns = {column["name"] for column in inspector.get_columns("admin_user")}
    session_columns = {column["name"] for column in inspector.get_columns("admin_session")}
    customer_columns = (
        {column["name"] for column in inspector.get_columns("customer")}
        if "customer" in inspector.get_table_names()
        else set()
    )

    with db.engine.begin() as connection:
        if "two_factor_secret" not in admin_columns:
            connection.execute(text("ALTER TABLE admin_user ADD COLUMN two_factor_secret VARCHAR(255)"))
        if "api_token" not in admin_columns:
            connection.execute(text("ALTER TABLE admin_user ADD COLUMN api_token VARCHAR(100)"))
        if "unusual" not in session_columns:
            connection.execute(text("ALTER TABLE admin_session ADD COLUMN unusual BOOLEAN DEFAULT false"))
        if "last_login" not in customer_columns:
            connection.execute(text("ALTER TABLE customer ADD COLUMN last_login TIMESTAMP"))
        if "pending_email" not in customer_columns:
            connection.execute(text("ALTER TABLE customer ADD COLUMN pending_email VARCHAR(200)"))
        if "email_token" not in customer_columns:
            connection.execute(text("ALTER TABLE customer ADD COLUMN email_token VARCHAR(100)"))
        if "api_token" not in customer_columns:
            connection.execute(text("ALTER TABLE customer ADD COLUMN api_token VARCHAR(100)"))

        if "catalog_product" in inspector.get_table_names():
            product_cols = {c["name"] for c in inspector.get_columns("catalog_product")}
            if "image_url" not in product_cols:
                connection.execute(text("ALTER TABLE catalog_product ADD COLUMN image_url VARCHAR(500)"))
            if "price" not in product_cols:
                connection.execute(text("ALTER TABLE catalog_product ADD COLUMN price VARCHAR(40)"))
            if "description" not in product_cols:
                connection.execute(text("ALTER TABLE catalog_product ADD COLUMN description TEXT"))
            if "buy_url" not in product_cols:
                connection.execute(text("ALTER TABLE catalog_product ADD COLUMN buy_url VARCHAR(500)"))

        if "catalog_livery" in inspector.get_table_names():
            livery_cols = {c["name"] for c in inspector.get_columns("catalog_livery")}
            if "image_url" not in livery_cols:
                connection.execute(text("ALTER TABLE catalog_livery ADD COLUMN image_url VARCHAR(500)"))


def ensure_bootstrap_owner():
    username = os.getenv("ADMIN_USERNAME")
    password = os.getenv("ADMIN_PASSWORD")
    if not username or not password:
        return

    from .extensions import bcrypt
    from .models import AdminUser

    email = os.getenv("ADMIN_EMAIL", f"{username}@rsgsoftware.com").lower()
    admin = AdminUser.query.filter_by(username=username).first()
    if admin is None:
        admin = AdminUser.query.filter_by(email=email).first()

    if admin is None:
        admin = AdminUser(
            username=username,
            email=email,
            password_hash=bcrypt.generate_password_hash(password).decode("utf-8"),
            role="Owner",
            enabled=True,
        )
        db.session.add(admin)
    else:
        admin.username = username
        admin.email = email
        admin.role = "Owner"
        admin.enabled = True
        if os.getenv("ADMIN_RESET_PASSWORD", "").lower() == "true":
            admin.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")

    db.session.commit()


def create_app():
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.getenv(
        "SECRET_KEY", "development-only-change-this-secret"
    )

    database_url = os.getenv("DATABASE_URL", "sqlite:///database.db")

    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.admin_login"
    csrf.init_app(app)
    limiter.init_app(app)

    from .models import AdminUser

    @login_manager.user_loader
    def load_user(user_id):
        return AdminUser.query.get(int(user_id))

    from .views import views
    app.register_blueprint(views)

    from .auth import auth
    app.register_blueprint(auth)

    from .admin import admin
    app.register_blueprint(admin)

    from .api import api
    app.register_blueprint(api)
    csrf.exempt(api)

    with app.app_context():
        db.create_all()
        upgrade_database_schema()
        ensure_bootstrap_owner()

    return app