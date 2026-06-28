"""Authentication service for the demo app.

Handles user login, token issuance, and session validation.
"""

import hashlib
from datetime import datetime

DEFAULT_TOKEN_TTL = 3600
MAX_LOGIN_ATTEMPTS = 5


def hash_password(password: str, salt: str) -> str:
    """Return a salted SHA256 hash of the password."""
    return hashlib.sha256((salt + password).encode()).hexdigest()


class AuthService:
    """Issues and validates authentication tokens for API endpoints."""

    def __init__(self, secret: str):
        self.secret = secret
        self._sessions: dict[str, datetime] = {}

    def login(self, username: str, password: str) -> str:
        """Authenticate a user and return a session token."""
        token = hash_password(username + password, self.secret)
        self._sessions[token] = datetime.now()
        return token

    def validate(self, token: str) -> bool:
        """Return True if the token corresponds to an active session."""
        return token in self._sessions
