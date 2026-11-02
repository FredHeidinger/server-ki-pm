"""Django-Einstellungen für Server-KI-PM.

Grundsätze: eine SQLite-Datei außerhalb des Quellcodes im Ordner ``daten`` (nicht im
Repository), Geheimnis aus Umgebungsvariable oder lokaler Datei, keine
Django-Verwaltungsoberfläche, serverseitig gerenderte Seiten.
"""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATEN_DIR = Path(os.environ.get("KIPM_DATEN_DIR", BASE_DIR / "daten"))
DATEN_DIR.mkdir(parents=True, exist_ok=True)


def _geheimnis() -> str:
    """Geheimer Schlüssel aus Umgebung oder lokaler Datei; wird einmalig erzeugt."""
    aus_umgebung = os.environ.get("KIPM_SECRET_KEY")
    if aus_umgebung:
        return aus_umgebung
    datei = DATEN_DIR / ".secret_key"
    if not datei.exists():
        datei.write_text(secrets.token_urlsafe(50), encoding="utf-8")
    return datei.read_text(encoding="utf-8").strip()


SECRET_KEY = _geheimnis()
DEBUG = os.environ.get("KIPM_DEBUG", "1") == "1"
ALLOWED_HOSTS = [
    h for h in os.environ.get("KIPM_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h
]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "steuerung",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "kipm.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "steuerung.kontext.projektfilter",
            ],
        },
    },
]

WSGI_APPLICATION = "kipm.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATEN_DIR / "kipm.sqlite3",
        # Tests gegen eine Datei statt Speicher, damit Sperren wie im Betrieb wirken.
        "TEST": {"NAME": DATEN_DIR / "test_kipm.sqlite3"},
        "OPTIONS": {
            # IMMEDIATE: Schreibtransaktionen sperren sofort; passt zu exklusiven Übernahmen.
            "transaction_mode": "IMMEDIATE",
            "init_command": (
                "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;"
            ),
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "anmelden"
LOGIN_REDIRECT_URL = "posteingang"
LOGOUT_REDIRECT_URL = "anmelden"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
CSRF_TRUSTED_ORIGINS = [
    f"https://{h}" for h in ALLOWED_HOSTS if h not in ("localhost", "127.0.0.1")
]
# Öffentliche Adresse für KI-Programme (z. B. Tunnel mit echtem Zertifikat); leer = Heimnetzadresse.
KIPM_OEFFENTLICHE_URL = os.environ.get("KIPM_OEFFENTLICHE_URL", "")

if os.environ.get("KIPM_HINTER_PROXY") == "1":
    # Caddy terminiert TLS und setzt X-Forwarded-Proto; nur so erkennt Django HTTPS.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LANGUAGE_CODE = "de"
TIME_ZONE = "Europe/Vienna"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = DATEN_DIR / "static"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
