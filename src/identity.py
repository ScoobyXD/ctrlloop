"""Shared email normalization and salted personal/company password credentials."""
import asyncio
import base64
import hashlib
import hmac
import re
import secrets


def normalize_email(value):
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", value):
        raise ValueError('Enter a valid email address')
    return value


def token_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def password_hash(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 500:
        raise ValueError('Password must contain 12–500 characters')
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(value.encode('utf-8'), salt=salt, n=32768, r=8, p=1,
                            maxmem=64 * 1024 * 1024, dklen=32)
    return 'scrypt$32768$8$1$' + base64.b64encode(salt).decode() + '$' + base64.b64encode(digest).decode()


def verify_password(value, encoded):
    try:
        if not isinstance(value, str) or len(value) > 500: return False
        algorithm, n, r, p, salt, expected = encoded.split('$')
        if (algorithm, n, r, p) != ('scrypt', '32768', '8', '1'): return False
        digest = hashlib.scrypt(value.encode('utf-8'), salt=base64.b64decode(salt, validate=True),
                                n=int(n), r=int(r), p=int(p), maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(digest, base64.b64decode(expected, validate=True))
    except (ValueError, TypeError, AttributeError):
        return False


password_slots = asyncio.Semaphore(4)


async def hash_password(value):
    async with password_slots:
        return await asyncio.to_thread(password_hash, value)


async def check_password(value, encoded):
    async with password_slots:
        return await asyncio.to_thread(verify_password, value, encoded)
