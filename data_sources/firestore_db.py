"""
Firestore-backed persistent state for the paper trading bot.
Falls back gracefully if Firestore is unavailable.

Auth: set env vars (FIREBASE_*), or GOOGLE_APPLICATION_CREDENTIALS pointing to the .json file.
"""

import os
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

COLLECTION = "bot_state"
DOCUMENT = "simulator"

_db = None
_enabled = False


def _init():
    global _db, _enabled
    if _db is not None:
        return _enabled

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        # Try env-var credentials first (FIREBASE_* vars)
        project_id = os.environ.get("FIREBASE_PROJECT_ID")
        if project_id:
            service_account_info = {
                "type": "service_account",
                "project_id": project_id,
                "private_key_id": os.environ.get("FIREBASE_PRIVATE_KEY_ID", ""),
                "private_key": os.environ.get("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n"),
                "client_email": os.environ.get("FIREBASE_CLIENT_EMAIL", ""),
                "client_id": os.environ.get("FIREBASE_CLIENT_ID", ""),
                "auth_uri": os.environ.get("FIREBASE_AUTH_URI", "https://accounts.google.com/o/oauth2/auth"),
                "token_uri": os.environ.get("FIREBASE_TOKEN_URI", "https://oauth2.googleapis.com/token"),
                "auth_provider_x509_cert_url": os.environ.get("FIREBASE_AUTH_PROVIDER_X509_CERT_URL", "https://www.googleapis.com/oauth2/v1/certs"),
                "client_x509_cert_url": os.environ.get("FIREBASE_CLIENT_X509_CERT_URL", ""),
                "universe_domain": "googleapis.com",
            }
            cred = credentials.Certificate(service_account_info)
        else:
            # Fall back to GOOGLE_APPLICATION_CREDENTIALS file
            cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            if not cred_path or not os.path.exists(cred_path):
                logger.warning("No Firebase credentials found — Firestore disabled")
                _enabled = False
                return False
            cred = credentials.Certificate(cred_path)

        firebase_admin.initialize_app(cred)
        _db = firestore.client()
        _enabled = True
        logger.info("Firestore connected (project=%s)", project_id or "from file")
    except Exception as e:
        logger.error("Firestore init failed: %s", e)
        _enabled = False

    return _enabled


def get_state() -> Optional[dict]:
    if not _init():
        return None
    try:
        doc = _db.collection(COLLECTION).document(DOCUMENT).get()
        if doc.exists:
            return doc.to_dict()
        return None
    except Exception as e:
        logger.error("Firestore read error: %s", e)
        return None


def save_state(data: dict) -> bool:
    if not _init():
        return False
    try:
        _db.collection(COLLECTION).document(DOCUMENT).set(data)
        return True
    except Exception as e:
        logger.error("Firestore write error: %s", e)
        return False


def is_enabled() -> bool:
    return _enabled
