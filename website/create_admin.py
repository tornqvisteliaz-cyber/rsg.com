from website import create_app, db
from website.models import AdminUser
from website.extensions import bcrypt

app = create_app()

with app.app_context():
    # Ta bort gammal admin om den finns (valfritt)
    existing = AdminUser.query.filter_by(email="Tornqvisteliaz@gmail.com").first()
    if existing:
        db.session.delete(existing)
        db.session.commit()

    password_hash = bcrypt.generate_password_hash("Halsband1983").decode("utf-8")

    admin = AdminUser(
        username="eliaz",                          # Det du loggar in med
        email="Tornqvisteliaz@gmail.com",
        password_hash=password_hash,
        role="Owner",                              # Högsta behörighet
        enabled=True
    )

    db.session.add(admin)
    db.session.commit()

    print("Owner admin created successfully!")
    print("Username: eliaz")
    print("Password: Halsband1983")