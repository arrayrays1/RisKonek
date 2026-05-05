
# python create_admin.py
from app.database import SessionLocal
from app.models import User, UserRole
from app.auth import hash_password

db = SessionLocal()

# Check if admin already exists
existing = db.query(User).filter(User.username == "admin").first()
if existing:
    print("Admin account already exists.")
else:
    admin = User(
        username="admin",
        email="admin@riskonek.com",
        password_hash=hash_password("admin123"),
        role=UserRole.admin,
        is_active=True
    )
    db.add(admin)
    db.commit()
    print("✓ Admin account created.")
    print("  Username: admin")
    print("  Password: admin123")
    print("  Change this password before deploying!")

existing = db.query(User).filter(User.username == "cfau").first()
if existing:
    print("Cfau account already exists.")
else:
    cfau = User(
        username="cfau",
        email="cfau@riskonek.com",
        password_hash=hash_password("cfau123"),
        role=UserRole.cfau_oic,
        is_active=True
    )
    db.add(cfau)
    db.commit()
    print("✓ Cfau account created.")
    print("  Username: cfau")
    print("  Password: cfau123")
    print("  Change this password before deploying!")
db.close()