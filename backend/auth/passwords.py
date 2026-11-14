"""
Password hashing and password policy (standard library only)
=============================================================

Hashes use **scrypt** from ``hashlib`` (memory-hard, available in every
CPython build on Windows/Linux/macOS since 3.6), so no extra dependency is
needed for an offline install. Stored format::

    scrypt$<n>$<r>$<p>$<salt-b64>$<hash-b64>

The parameters are stored with each hash, so they can be raised later without
invalidating existing passwords (``needs_rehash`` tells the login path to
upgrade a hash transparently on the next successful login).

Plain-text passwords are never logged, stored, or returned by any API.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# scrypt cost: 2**14 * 8 * 128 bytes = 16 MiB of memory per hash, ~50-100 ms on
# a laptop. Strong against offline cracking, cheap enough for interactive login.
_N, _R, _P = 2 ** 14, 8, 1
_DKLEN = 32
_MAXMEM = 64 * 1024 * 1024

MIN_LENGTH_DEFAULT = 10
MAX_LENGTH = 256  # scrypt has no length limit; this only bounds request size


def _b64(b: bytes) -> str:
    """Standard base64 text of bytes."""
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str) -> str:
    """Hash ``password`` with a fresh 16-byte random salt."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check of ``password`` against a stored hash. Any
    malformed/unknown hash format verifies as False (never raises)."""
    if not stored or not isinstance(password, str):
        return False
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
                            dklen=len(expected), maxmem=_MAXMEM)
        return hmac.compare_digest(dk, expected)
    except Exception:  # noqa: BLE001 - corrupt hash == wrong password
        return False


def needs_rehash(stored: str | None) -> bool:
    """True when ``stored`` was made with weaker parameters than the current ones."""
    try:
        algo, n, r, p, *_ = (stored or "").split("$")
        return algo != "scrypt" or (int(n), int(r), int(p)) < (_N, _R, _P)
    except ValueError:
        return True


# A hash of a random password, verified against when a login names an unknown
# user, so "no such user" and "wrong password" take the same time.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def password_problems(password: str, *, username: str = "", min_length: int = MIN_LENGTH_DEFAULT) -> list[str]:
    """Human-readable reasons ``password`` is not acceptable (empty list = OK).

    Policy (deliberately simple, in line with NIST SP 800-63B): a minimum
    length, a maximum length, not the username, and at least two character
    classes so trivially uniform strings ("aaaaaaaaaa", "1234567890") fail."""
    problems: list[str] = []
    if not isinstance(password, str) or len(password) < min_length:
        problems.append(f"must be at least {min_length} characters long")
        return problems
    if len(password) > MAX_LENGTH:
        problems.append(f"must be at most {MAX_LENGTH} characters long")
    if username and password.strip().lower() == username.strip().lower():
        problems.append("must not be the same as the username")
    classes = sum([
        any(c.islower() for c in password), any(c.isupper() for c in password),
        any(c.isdigit() for c in password), any(not c.isalnum() for c in password),
    ])
    if classes < 2:
        problems.append("must mix at least two of: lowercase, uppercase, digits, symbols")
    return problems


def generate_password(length: int = 16) -> str:
    """A random password that satisfies ``password_problems`` (used by the CLI
    for test accounts and admin-initiated resets when no password is given)."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length - 2)) + secrets.choice("!#%+-=?@") + secrets.choice("23456789")
        if not password_problems(pw):
            return pw
