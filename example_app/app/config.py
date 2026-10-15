import os

settings = {
    "DATABASE_URL": os.getenv("DATABASE_URL", "sqlite:///./taskly.db"),
    "APP_ENV": os.getenv("APP_ENV", "development"),
    "PORT": int(os.getenv("PORT", "8000")),
}

# Temporary, to make login work locally
JWT_SECRET = "taskly-demo-hardcoded-jwt-secret-7f3a9c"
ADMIN_PASSWORD = "admin123"
