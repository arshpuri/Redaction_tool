"""Context-first PII detection.

Every rule in PII_REGISTRY looks at a single leaf Node (its value, its label,
whether it's an atomic structured value vs. free prose, and any row-level
context passed down by the walker) and returns:

    (spans, conflicts)
    spans:     [(start, end, reason), ...]      offsets into node.value
    conflicts: ["human-readable message", ...]  raised when a label strongly
                                                 implies a type but the value
                                                 doesn't back it up

Value-level pattern/NER confirmation is delegated to Presidio, run once per
leaf (a single analyze() call covers every pattern-backed type at once), with
node.label passed in as Presidio's `context` hint for confidence boosting.
Presidio ships India-specific recognizers (PAN, Aadhaar) which is why it's
used here instead of hand-rolled regex for those types.

Full-name detection deliberately does NOT lean on spaCy's PERSON NER as its
primary signal — spaCy's English-trained model under-recognizes Indian names.
Instead it uses two NER-independent paths first (context+shape, and
honorific/kinship patterns like "Shri"/"S/o"), and only falls back to NER
when some other supporting context is already present (never a bare NER hit).
"""

import re
from dataclasses import dataclass

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import (
    InAadhaarRecognizer,
    InPanRecognizer,
    InPassportRecognizer,
    InVehicleRegistrationRecognizer,
    InVoterRecognizer,
)


@dataclass
class Span:
    start: int
    end: int
    pii_type: str
    text: str
    reason: str


@dataclass
class Conflict:
    node: object
    pii_type: str
    message: str


# ---------------------------------------------------------------------------
# Presidio engine (local spaCy backend, India-region recognizers registered)
# ---------------------------------------------------------------------------

_NLP_CONF = {
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": "en_core_web_lg"}],
}

ALL_ENTITIES = [
    "EMAIL_ADDRESS", "PHONE_NUMBER", "IN_PAN", "IN_AADHAAR",
    "CREDIT_CARD", "IP_ADDRESS", "PERSON",
]

_analyzer = None


def get_analyzer():
    global _analyzer
    if _analyzer is not None:
        return _analyzer
    provider = NlpEngineProvider(nlp_configuration=_NLP_CONF)
    nlp_engine = provider.create_engine()
    registry = RecognizerRegistry()
    registry.load_predefined_recognizers(nlp_engine=nlp_engine)

    pan_recognizer = InPanRecognizer()
    # Presidio's "PAN (Low)" pattern uses an unbounded lookahead
    # (`(?=.*?[0-9]{4})` scans the whole *remaining text*, not the 10-char
    # candidate itself), so any ordinary word gets flagged once a PAN-context
    # word appears anywhere earlier in the same paragraph. Drop it; the
    # High/Medium patterns are properly `\b`-bounded format checks.
    pan_recognizer.patterns = [p for p in pan_recognizer.patterns if "Low" not in p.name]

    for rec in (pan_recognizer, InAadhaarRecognizer(), InPassportRecognizer(),
                InVehicleRegistrationRecognizer(), InVoterRecognizer()):
        registry.add_recognizer(rec)
    _analyzer = AnalyzerEngine(nlp_engine=nlp_engine, registry=registry)
    return _analyzer


# ---------------------------------------------------------------------------
# Context vocabulary + negative lists
# ---------------------------------------------------------------------------

CONTEXT_VOCAB = {
    "name": {"name", "contact name", "customer name", "shareholder", "promoter",
             "director", "signatory", "applicant", "guardian", "authorised signatory",
             "authorized signatory", "chief executive officer", "ceo",
             "chief financial officer", "cfo", "company secretary",
             "key managerial personnel", "compliance officer"},
    "email": {"email", "e-mail", "mail id", "mail"},
    "phone": {"phone", "mobile", "contact no", "contact number", "tel", "telephone", "fax"},
    "address": {"address", "residence", "residential", "correspondence"},
    "pan": {"pan"},
    "aadhaar": {"aadhaar", "aadhar", "uid"},
    "card": {"card", "account no", "account number", "iban", "bank account", "card number"},
    "dob": {"dob", "date of birth", "born"},
    "ip": {"ip", "ip address"},
}

# transactional/registration identifiers — never redacted, even if numeric
NEGATIVE_ID_LABELS = {
    "order", "order #", "order no", "order no.", "order number",
    "ticket", "ticket #", "ticket no", "ticket no.", "ticket number",
    "plan id", "din", "cin", "isin",
}

# a company's own registered/corporate address is not personal data
NEGATIVE_ADDRESS_PHRASES = (
    "registered office", "corporate office", "regd. office", "regd office",
    "registered & corporate office",
)

# filters corporate/entity names out of "Name of Promoter/Shareholder" columns.
# Beyond formal legal suffixes, includes common Indian trade-name descriptors
# (e.g. "Kushal Electricals", "Waterloo Motors" are partnership firms, not
# people, despite having no "Ltd/Pvt/LLP" suffix at all) — a heuristic, not
# exhaustive, but covers the patterns actually observed in filing documents.
ORG_SUFFIX_RE = re.compile(
    r"\b(limited|ltd\.?|private|pvt\.?|llp|inc\.?|corp(?:oration)?|co\.?|bank|trust|huf|"
    r"ventures?|holdings?|fund|foundation|society|association|enterprises?|industries|"
    r"group|company|plc|llc|electricals?|motors?|traders?|agencies|stores?|textiles?|"
    r"exports?|imports?|distributors?|automobiles?|engineering|constructions?|builders?|"
    r"suppliers?|hardware|electronics|garments?|associates?|brothers?|sons)\b",
    re.IGNORECASE,
)

NAME_SHAPE_RE = re.compile(r"^(?:[A-Z][a-zA-Z'.\-]*\s+){1,3}[A-Z][a-zA-Z'.\-]*$")

HONORIFIC_RE = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Shri|Smt|Kumari|Sri)\.?\s+([A-Z][a-zA-Z'.\-]+(?:\s+[A-Z][a-zA-Z'.\-]+){0,3})"
)
KINSHIP_RE = re.compile(
    r"\b(?:S/o|D/o|W/o|C/o)\.?\s*([A-Z][a-zA-Z'.\-]+(?:\s+[A-Z][a-zA-Z'.\-]+){0,3})",
    re.IGNORECASE,
)

_DATE_RE = re.compile(
    r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}|"
    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*,?\s+\d{4})\b",
    re.IGNORECASE,
)


_VOCAB_PATTERNS = {
    type_key: [re.compile(r"\b" + re.escape(w) + r"\b", re.IGNORECASE) for w in words]
    for type_key, words in CONTEXT_VOCAB.items()
}


def _label_matches(label, type_key):
    if not label:
        return False
    # word-boundary match, not raw substring — a naive `"tel" in label`-style
    # check would match "location" inside "Allocation" or "tel" inside
    # "Details", both observed as real false positives in RHP glossary tables.
    # Normalize snake_case/kebab-case first: regex \b treats "_" as a word
    # character, so "customer_name" has no boundary before "name" otherwise.
    normalized = re.sub(r"[_\-]", " ", label)
    return any(p.search(normalized) for p in _VOCAB_PATTERNS[type_key])


def _label_is_negative_id(label):
    if not label:
        return False
    return label.strip().lower() in NEGATIVE_ID_LABELS


# ---------------------------------------------------------------------------
# Per-type rules — PII_REGISTRY[type] = fn(node, by_type, row_context)
# ---------------------------------------------------------------------------


# Presidio's PhoneRecognizer (via libphonenumber) scores an unlabeled match
# at a flat 0.4 with no way to distinguish a genuine bare phone number from
# a coincidentally phone-shaped reference number — both score identically.
# Two shapes observed as real false positives in the RHP: a standard/spec
# number like "IATF 16949:2016" (digits immediately followed by ":YYYY"),
# and a circular reference like "20220803-40" (an 8-digit date + suffix).
_STANDARD_SPEC_SUFFIX_RE = re.compile(r"^\s*:\s*(19|20)\d{2}\b")
_DATE_REFERENCE_RE = re.compile(r"^(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])-\d+$")


def _looks_like_reference_number(full_text, start, end):
    matched = full_text[start:end]
    if _DATE_REFERENCE_RE.match(matched):
        return True
    return bool(_STANDARD_SPEC_SUFFIX_RE.match(full_text[end:end + 8]))


def _generic_entity_rule(type_key, entity_name):
    def rule(node, by_type, row_context):
        text, label = node.value, node.label
        atomic = node.meta.get("atomic", False)
        ctx = _label_matches(label, type_key)
        hits = [(r.start, r.end) for r in by_type.get(entity_name, []) if r.score >= 0.4]
        if type_key == "phone":
            hits = [(s, e) for s, e in hits if not _looks_like_reference_number(text, s, e)]
        spans = [(s, e, "context+value" if ctx else "value-self-evident") for s, e in hits]
        conflicts = []
        if not hits and ctx and atomic:
            conflicts.append(f"label matched '{type_key}' but value has no matching pattern")
        return spans, conflicts
    return rule


def _dob_rule(node, by_type, row_context):
    text, label = node.value, node.label
    atomic = node.meta.get("atomic", False)
    if not _label_matches(label, "dob"):
        return [], []
    m = _DATE_RE.search(text)
    if m:
        return [(*m.span(), "context+value")], []
    if atomic:
        return [], ["label matched 'dob' but value has no parseable date"]
    return [], []


def _looks_like_address_value(text):
    # a real street address almost always has a house/pin/area number
    # somewhere; a job title or other unlabeled column (e.g. "Chairman and
    # Executive Director") never does. Only gates the *inherited* (no
    # explicit label) path — an explicitly address-labeled field is trusted
    # on context alone regardless of shape, per the calibration tier.
    return bool(re.search(r"\d", text)) and ("," in text or len(text.split()) >= 4)


def _address_rule(node, by_type, row_context):
    text, label = node.value, node.label
    ctx = _label_matches(label, "address")
    inherited = bool(row_context and row_context.get("has_person") and label is None)
    if not (ctx or inherited):
        return [], []
    if any(neg in text.lower() for neg in NEGATIVE_ADDRESS_PHRASES):
        return [], []
    if inherited and not _looks_like_address_value(text):
        return [], []
    return [(0, len(text), "context" if ctx else "row-context")], []


def _name_rule(node, by_type, row_context):
    text, label = node.value, node.label
    atomic = node.meta.get("atomic", False)
    name_ctx = _label_matches(label, "name")
    spans = []

    # Path A — context + shape, NER-independent (robust for Indian names)
    if name_ctx and atomic and NAME_SHAPE_RE.match(text.strip()) and not ORG_SUFFIX_RE.search(text):
        spans.append((0, len(text), "name-shape"))

    # Path B — honorific / kinship markers, NER-independent, works in prose too
    for rx, reason in ((HONORIFIC_RE, "name-honorific"), (KINSHIP_RE, "name-kinship")):
        for m in rx.finditer(text):
            if not ORG_SUFFIX_RE.search(m.group(1)):
                spans.append((*m.span(1), reason))

    # Path C — NER fallback, only trusted when the *node itself* is already a
    # name-labeled field or a signature zone. Deliberately does NOT trust "an
    # honorific exists somewhere in this text" as support for an unrelated NER
    # hit elsewhere in the same paragraph — that let PERSON mis-tags on nearby
    # place names (e.g. "resident of Kothrud") ride along on an unrelated
    # "Smt ..." earlier in the same sentence. Honorific/kinship-adjacent names
    # are already captured precisely by Path B above.
    has_support = name_ctx or node.meta.get("zone") == "signature_block"
    if has_support:
        for r in by_type.get("PERSON", []):
            if r.score >= 0.5 and not ORG_SUFFIX_RE.search(text[r.start:r.end]):
                spans.append((r.start, r.end, "name-ner+context"))

    return spans, []


PII_REGISTRY = {
    "email": _generic_entity_rule("email", "EMAIL_ADDRESS"),
    "phone": _generic_entity_rule("phone", "PHONE_NUMBER"),
    "pan": _generic_entity_rule("pan", "IN_PAN"),
    "aadhaar": _generic_entity_rule("aadhaar", "IN_AADHAAR"),
    "card": _generic_entity_rule("card", "CREDIT_CARD"),
    "ip": _generic_entity_rule("ip", "IP_ADDRESS"),
    "dob": _dob_rule,
    "address": _address_rule,
    "name": _name_rule,
}


# ---------------------------------------------------------------------------
# Span accumulation: widest-span-wins containment resolution
# ---------------------------------------------------------------------------

def merge_span(spans, s, e, pii_type, reason, text):
    if any(sp.start <= s and e <= sp.end for sp in spans):
        return  # already covered by an existing (equal or wider) span
    spans[:] = [sp for sp in spans if not (s <= sp.start and sp.end <= e)]  # this one subsumes them
    if any(not (e <= sp.start or s >= sp.end) for sp in spans):
        return  # partial (non-nesting) overlap — leave existing span alone, skip this one
    spans.append(Span(s, e, pii_type, text[s:e], reason))


def detect_leaf(node, row_context=None):
    """Run the full registry against one leaf node. Returns (spans, conflicts)."""
    text = node.value
    if text is None or not text.strip():
        return [], []
    if _label_is_negative_id(node.label):
        return [], []

    analyzer = get_analyzer()
    results = analyzer.analyze(
        text=text, language="en", entities=ALL_ENTITIES,
        context=[node.label] if node.label else None,
    )
    by_type = {}
    for r in results:
        by_type.setdefault(r.entity_type, []).append(r)

    spans = []
    conflicts = []
    for type_key, rule in PII_REGISTRY.items():
        rule_spans, rule_conflicts = rule(node, by_type, row_context)
        for s, e, reason in rule_spans:
            if s < e:
                merge_span(spans, s, e, type_key, reason, text)
        conflicts.extend(Conflict(node, type_key, msg) for msg in rule_conflicts)
    return spans, conflicts
