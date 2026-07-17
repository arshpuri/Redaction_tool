"""Local fake-value generation with real->fake referential consistency.

Every (pii_type, normalized real value) maps to exactly one fake value, no
matter how many times or where it appears in the document. Consistency is
achieved by seeding Faker deterministically from a hash of the (type, value)
pair, so re-running the tool on the same input reproduces the same fakes
without persisting any state to disk — plus an in-memory cache per run.

Email policy: the local part is always faked; the domain is kept as-is
unless it's a free-mail provider (gmail/yahoo/...), in which case both parts
are faked. A corporate mail domain isn't personal data on its own.
"""

import hashlib
import re

from faker import Faker

_FAKE = Faker("en_IN")

_FREE_MAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com",
    "rediffmail.com", "protonmail.com", "icloud.com", "live.com", "aol.com",
}

_cache = {}


def _seed_for(pii_type, normalized_value):
    digest = hashlib.sha256(f"{pii_type}:{normalized_value}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def normalize(pii_type, value):
    v = value.strip()
    if pii_type in ("name", "address"):
        return re.sub(r"\s+", " ", v).lower()
    if pii_type in ("phone", "card", "aadhaar"):
        return re.sub(r"\D", "", v)
    if pii_type == "pan":
        return re.sub(r"[^A-Za-z0-9]", "", v).upper()
    if pii_type == "email":
        return v.lower()
    return v


def _fake_pan(seed):
    _FAKE.seed_instance(seed)
    letters1 = "".join(_FAKE.random_uppercase_letter() for _ in range(5))
    digits = "".join(str(_FAKE.random_digit()) for _ in range(4))
    letter2 = _FAKE.random_uppercase_letter()
    return f"{letters1}{digits}{letter2}"


def _fake_email(seed, real_value):
    _FAKE.seed_instance(seed)
    domain = real_value.split("@")[-1].lower() if "@" in real_value else ""
    if domain and domain not in _FREE_MAIL_DOMAINS:
        local = _FAKE.user_name()
        return f"{local}@{domain}"
    return _FAKE.email()


_GENERATORS = {
    "name": lambda seed, real: (_FAKE.seed_instance(seed), _FAKE.name())[1],
    "email": lambda seed, real: _fake_email(seed, real),
    "phone": lambda seed, real: (_FAKE.seed_instance(seed), _FAKE.phone_number())[1],
    "address": lambda seed, real: (_FAKE.seed_instance(seed), _FAKE.address().replace("\n", ", "))[1],
    "pan": lambda seed, real: _fake_pan(seed),
    "aadhaar": lambda seed, real: (_FAKE.seed_instance(seed), _FAKE.aadhaar_id())[1],
    "card": lambda seed, real: (_FAKE.seed_instance(seed), _FAKE.credit_card_number())[1],
    "dob": lambda seed, real: (_FAKE.seed_instance(seed), _FAKE.date_of_birth(minimum_age=18, maximum_age=75).strftime("%d-%m-%Y"))[1],
    "ip": lambda seed, real: (_FAKE.seed_instance(seed), _FAKE.ipv4_public())[1],
}


def fake_for(pii_type, real_value):
    key = (pii_type, normalize(pii_type, real_value))
    if key in _cache:
        return _cache[key]
    seed = _seed_for(*key)
    gen = _GENERATORS.get(pii_type)
    fake_value = gen(seed, real_value) if gen else f"[REDACTED:{pii_type.upper()}]"
    _cache[key] = fake_value
    return fake_value


def reset_cache():
    _cache.clear()
