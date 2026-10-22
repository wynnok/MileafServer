"""Single-site password authentication; business data never enters local state."""

import fcntl
import hashlib
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, SecretStr

from mileaf.dependencies import get_now
from mileaf.errors import APIError, ErrorCode


class TokenRecord(BaseModel):
    digest: str
    created_at: datetime
    expires_at: datetime


class AuthState(BaseModel):
    password_hash: str
    tokens: list[TokenRecord]
    created_at: datetime


class Credentials(BaseModel):
    password: SecretStr


class LoginResponse(BaseModel):
    token: str
    expires_at: datetime


def unauthorized() -> APIError:
    return APIError(401, ErrorCode.UNAUTHORIZED, "Invalid or expired credentials")


class AuthStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.hasher = PasswordHasher()
        self.login_lock = Lock()
        self.failures: dict[str, list[datetime]] = {}

    @contextmanager
    def locked(self) -> Iterator[None]:
        # Keep a stable lock inode: state.json itself is replaced on each write.
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            descriptor = os.open(lock_path, os.O_RDWR)
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def read(self) -> AuthState | None:
        try:
            return AuthState.model_validate_json(self.path.read_bytes())
        except FileNotFoundError:
            return None

    def write(self, state: AuthState) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(16)}")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(state.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def setup(self, password: str, now: datetime) -> None:
        with self.locked():
            if self.path.exists():
                raise APIError(409, ErrorCode.ALREADY_INITIALIZED, "Already initialized")
            if len(password) < 10:
                raise APIError(
                    400, ErrorCode.INVALID_REQUEST, "Password must have at least 10 characters"
                )
            self.write(
                AuthState(password_hash=self.hasher.hash(password), tokens=[], created_at=now)
            )

    def login(self, password: str, ip: str, now: datetime) -> LoginResponse:
        with self.login_lock, self.locked():
            cutoff = now - timedelta(minutes=1)
            self.failures = {
                address: recent
                for address, failures in self.failures.items()
                if (recent := [time for time in failures if time > cutoff])
            }
            if len(self.failures.get(ip, [])) >= 5:
                raise APIError(429, ErrorCode.RATE_LIMITED, "Too many failed login attempts")
            state = self.read()
            valid = False
            if state is not None:
                with suppress(VerificationError):
                    valid = self.hasher.verify(state.password_hash, password)
            if not valid or state is None:
                self.failures.setdefault(ip, []).append(now)
                raise unauthorized()
            token = secrets.token_urlsafe(32)
            expires_at = now + timedelta(days=30)
            state.tokens = [record for record in state.tokens if record.expires_at > now][-9:]
            state.tokens.append(
                TokenRecord(
                    digest=hashlib.sha256(token.encode()).hexdigest(),
                    created_at=now,
                    expires_at=expires_at,
                )
            )
            self.write(state)
            return LoginResponse(token=token, expires_at=expires_at)

    def authenticate(self, token: str, now: datetime) -> None:
        with self.locked():
            state = self.read()
        digest = hashlib.sha256(token.encode()).hexdigest()
        if state is None or not any(
            secrets.compare_digest(record.digest, digest) and record.expires_at > now
            for record in state.tokens
        ):
            raise unauthorized()


def get_auth_store(request: Request) -> AuthStore:
    return request.app.state.auth_store


def require_bearer(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))
    ],
    store: Annotated[AuthStore, Depends(get_auth_store)],
    now: Annotated[datetime, Depends(get_now)],
) -> None:
    if request.url.path == "/v1/health" or request.url.path.startswith("/v1/auth/"):
        return
    if credentials is None or len(credentials.credentials.split()) != 1:
        raise unauthorized()
    store.authenticate(credentials.credentials, now)


router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/setup", openapi_extra={"security": [{}]})
def setup(
    credentials: Credentials,
    store: Annotated[AuthStore, Depends(get_auth_store)],
    now: Annotated[datetime, Depends(get_now)],
) -> dict[str, str]:
    store.setup(credentials.password.get_secret_value(), now)
    return {"status": "ok"}


@router.post("/login", openapi_extra={"security": [{}]})
def login(
    credentials: Credentials,
    request: Request,
    store: Annotated[AuthStore, Depends(get_auth_store)],
    now: Annotated[datetime, Depends(get_now)],
) -> LoginResponse:
    ip = request.client.host if request.client else "unknown"
    return store.login(credentials.password.get_secret_value(), ip, now)
