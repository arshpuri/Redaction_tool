# PII Redaction Tool

Structure-aware PII detection and redaction for JSON, DOCX, PDF, and TXT — one
detection core, four format adapters, 100% local execution.

## Why structure-aware, not regex-on-a-string-dump

A key/label matching a PII-type vocabulary is a strong signal regardless of
value format (a field named `email` is almost certainly an email address);
a bare 10-digit number floating in unstructured prose is not. Flattening a
document to one string and regexing it throws away exactly the context that
makes detection reliable. So every format is parsed into the same normalized
tree first, and detection runs once against that tree — context first (does
the key/column-header/label match a PII-type vocabulary?), then value-level
pattern/NER confirmation on the leaf.

## Architecture

```
redact.py       CLI entry point: detect format -> parse -> redact -> write
parsers.py      parse_json/docx/pdf/txt -> shared Node tree, + matching writers
detectors.py    PII_REGISTRY: one rule per type, Presidio-backed value checks
fake_data.py    Faker generators + real -> fake consistency mapping
evaluate.py     hand-labeled fixtures (all 4 formats) -> eval_report.md
```

### The Node tree

```
Node
  format      "json" | "docx" | "pdf" | "txt"
  node_type   object/array/field (json) · paragraph/table_cell/header/footer (docx)
              · page/line/table_cell (pdf) · line/pseudo_field (txt)
  label       the CONTEXT SIGNAL: json key, docx/pdf column header or matched
              "Label:" prefix, txt pseudo-field name — or None for free prose
  value       leaf text (None for containers)
  children    nested nodes
  source_ref  opaque handle back to the live object, for in-place writing
  meta        atomic flag, docx run refs, pdf bbox/word-offsets, etc.
```

A container's label propagates to its children — a DOCX/PDF table's column
header becomes every cell's label in that column, so a bare string under a
column literally titled "Email" still gets email-level context.

### Detection algorithm (`detectors.py`)

For every leaf, one Presidio `analyze()` call covers every pattern-backed
type at once (email/phone/PAN/Aadhaar/card/IP), with the node's label passed
in as Presidio's `context` hint. Each registry rule then applies:

- **context + value agree** → redact (highest confidence)
- **context present, value is free-text (address)** → redact (no rigid
  pattern to check against)
- **context present, but a pattern-backed type's value doesn't match** →
  raise a conflict, don't guess (see Stop conditions below)
- **no context, but the value is self-evident** (email/phone/PAN/Aadhaar/
  card/IP all have a reliable standalone format) → redact regardless
- **name**: see below — deliberately not NER-first

Overlapping spans within one leaf resolve by widest-span-wins containment
(a wider match subsumes a narrower one it fully contains; a partial,
non-nesting overlap is left alone) — this is what lets, e.g., a `name` match
from an honorific pattern and a wider `address` match coexist correctly
without corrupting write-back offsets.

### Why name detection avoids spaCy NER as its primary signal

The sample document (an Indian RHP/IPO filing) is almost entirely Indian
names, and spaCy's English-trained PERSON model has materially weaker recall
on those. Rather than accept that recall hit, name detection runs two
NER-independent paths first:

1. **Context + shape**: label matches a name vocabulary (name, promoter,
   director, shareholder, signatory, ...) *and* the value is a 2–4 word
   Title-Case span *and* it doesn't match a corporate-suffix/trade-name
   filter (`Ltd`, `Pvt`, `LLP`, `Trust`, `HUF`, but also common Indian
   trade-name words like `Electricals`, `Motors`, `Traders` — both observed
   as real false positives in testing, since related-party tables list
   proprietorship firms by trade name with no legal suffix at all).
2. **Honorific / kinship markers**: `Mr./Mrs./Shri/Smt./Kumari` or
   `S/o·D/o·W/o·C/o` immediately preceding a Title-Case span — a regex, not
   NER, so it's unaffected by the model's language bias.

spaCy PERSON NER is a **fallback only**, and only trusted when the node
itself is already name-labeled or in a signature-block zone — never on a
bare NER hit with no other support. This matches the calibration's
"high-confidence only" tier for names and was validated against a real
false positive: a paragraph containing "Signature Building" (a building
name) was initially treated as a signature zone purely because it contained
the word "signature", which let NER mis-tag nearby place names as PERSON.
The zone detector now requires kinship markers or "(un)authorised
signatory" specifically — not the bare word "signature"/"signed".

### Row-level context propagation

A DOCX/PDF table row is evaluated in two passes: first normally, then — if
any cell resolved to a person's name — sibling cells with no column label of
their own inherit "personal capacity" context for the address rule. This is
what correctly redacts a director's residential address (same row as their
name) while leaving the company's own "Registered Office" address alone
elsewhere in the same table, without needing a label on every column.

### Negative vocabulary (never redacted)

`Order #`, `Ticket #`, `Plan ID`, `DIN`, `CIN`, `ISIN` — transactional and
registration identifiers, checked before the positive registry runs at all.
Company/organization names are off entirely per the calibration tier; the
only interaction with addresses is that a company's own registered/corporate
office address is explicitly excluded even when it appears in an
otherwise-address-labeled field (`"registered office"`, `"corporate
office"`, ...).

### Presidio, and why

Presidio was adopted (per the "use it if it produces a better outcome"
call) mainly for its **India-region recognizers** (`InPanRecognizer`,
`InAadhaarRecognizer`, ...), which ship real validation logic rather than
hand-rolled regex, plus its context-boosting mechanism (`analyze(...,
context=[label])`). It runs entirely locally via a bundled spaCy model
(`en_core_web_lg`) — no network calls at runtime.

One bundled recognizer needed a fix: Presidio's `InPanRecognizer` ships a
"Low confidence" pattern with an **unbounded lookahead**
(`(?=.*?[0-9]{4})` scans the entire remaining text, not the 10-character
candidate itself), so in a long paragraph that merely *mentions* the word
"PAN" anywhere, any ordinary 10-letter word downstream could get flagged as
a PAN number once context-boosted. That pattern is explicitly stripped out
in `detectors.get_analyzer()`; the High/Medium patterns (properly
`\b`-bounded format checks) are kept.

Similarly, Presidio's phone recognizer (via `libphonenumber`) scores an
*unlabeled* match at a flat 0.4 regardless of whether it's a real phone
number or a coincidentally phone-shaped reference number — both score
identically, so no threshold can separate them. Two such false positives
were found empirically (`IATF 16949:2016`, a quality-standard number; a
`20220803-40`-style circular reference) and are now vetoed by shape: a
match immediately followed by `:YYYY`, or matching an 8-digit-date-plus-
suffix pattern, is rejected for the phone type specifically.

### Fake data & consistency (`fake_data.py`)

Every `(pii_type, normalized real value)` maps to exactly one fake value.
Normalization lowercases/collapses whitespace for names and addresses, and
strips non-digits for phone/PAN/card, so formatting variants of the same
real value collapse onto the same fake. Faker is seeded deterministically
from a hash of that key, so re-running the tool on the same input reproduces
the same fakes without persisting any state to disk.

Email policy: the local part is always faked; the domain is kept as-is
unless it's a free-mail provider (gmail/yahoo/outlook/...), in which case
both parts are faked — a corporate mail domain isn't personal data on its
own.

### Write-back per format

| Format | Approach |
|---|---|
| JSON | Splice the leaf string, assign back into the parsed object, `json.dump` |
| DOCX | Splice within `run.text` (not `paragraph.text`) to preserve bold/italic/style spans |
| PDF | pdfplumber for structure+bboxes on read; **PyMuPDF (fitz)** redaction annotations + replacement text on write |
| TXT | Splice at recorded character offsets, preserve line breaks |

A DOCX/PDF paragraph or line can contain multiple embedded `"Label: value"`
segments concatenated together (e.g. `"Telephone: 022-... Email:
x@y.com"`). Those get their own labeled, atomic `pseudo_field` child node so
context-dependent rules can fire on them — but their span offsets are
translated back into the *parent's* coordinate space and merged into one
combined span list before writing, rather than applied as two separate
passes. (Two sequential passes over the same paragraph would otherwise
corrupt each other's offsets once the first pass changes the text length.)

## Usage

```
python redact.py <input_path> [-o output_path] [--on-conflict ask|redact|skip] [--force]
```

- `--on-conflict ask` (default): stop and report label/value conflicts
  without writing output.
- `--on-conflict redact`: trust the label and redact the whole value anyway.
- `--on-conflict skip`: leave conflicting fields untouched, redact everything
  else.
- `--force`: overwrite an existing `<input>_redacted.<ext>`.

## Stop conditions (all verified against real input)

- **Extension/content mismatch** (e.g. a `.txt` file that's actually JSON) →
  refuses to parse, reports the mismatch.
- **Label/value conflict** (label says `email`, value has no email pattern)
  → reports every conflict and exits without writing, unless
  `--on-conflict` says otherwise.
- **Existing output file** → refuses to overwrite without `--force`.

## Known limitations

- **Name/address recall on unlabeled free prose**: a personal name or street
  address with no label, honorific, or table-column context, and not in a
  signature zone, will not be caught — this is a deliberate precision/recall
  trade-off for the "high-confidence only" name tier, not an oversight.
- **DOCX run-splicing**: if a single PII value happens to span more than two
  runs created by prior manual formatting, the middle runs are cleared and
  the value is written into the first/last — a rare edge case.
  
- **Business/proprietorship trade names** without a recognized legal suffix
  (e.g. "Kushal Electricals") are filtered from name columns via a curated
  keyword list, not a business-entity classifier — this is a heuristic, not
  exhaustive.
- **spaCy `en_core_web_lg`** is English-trained; PERSON NER (the fallback
  path only) still under-recognizes some Indian names. Mitigated, not
  eliminated, by making NER a fallback rather than the primary signal.
- **PDF-specific text fragmentation**: a name split across an unusual
  line/kerning break inside a narrow table column (observed once, in a
  condensed column on a scanned-looking page) can get redacted as two
  separate fragments instead of one clean name — still fully redacted, just
  less tidy output. Rare, and not a privacy failure.

### PDF write-back: verified against a real 128-page filing

The PDF path was validated end-to-end against a second real sample (a PDF
version of the same RHP), which surfaced and fixed several PDF-specific
issues beyond what the DOCX testing covered:

- **Table continuation across pages**: pdfplumber detects a table spanning a
  page break as a brand-new table with no header of its own — row 0 is
  ordinary DATA, not a header. Blindly trusting row 0 as the header (as the
  original implementation did) leaked garbage column labels into every row,
  which then coincidentally matched real vocabulary words (e.g. a row's
  leftover "...Director(s)" header text made an unrelated column inherit
  `name` context). Fixed by only adopting a row as a header if it actually
  *looks* like one (either a short ALL-CAPS phrase, or an exact match against
  a small set of generic column-header words like "Name"/"Address"/"DIN") —
  checked against every row in sequence, not just row 0.
- **"Term | Description" 2-column tables**: same fix as DOCX, ported to
  pdfplumber's grid representation (which pads merged cells with `None`,
  making raw column-count unreliable — so the check is based on how many
  rows carry exactly two non-empty cells, not raw grid width).
- **Single-column "tables" that are really wrapped headings**: pdfplumber
  can split one word-wrapped phrase (e.g. "Name, address, telephone and
  e-mail address of the Underwriters") across several pseudo-rows. One
  fragment was literally just the word "address" — a perfectly legitimate
  header word in isolation, which got adopted as a real column header and
  leaked address-context onto the unrelated fragments below. Fixed by never
  treating a single-column table as header-bearing at all (there's no
  second column to label anyway).
- **Row-context inheritance was too permissive**: the "sibling cell inherits
  personal-capacity context" rule (see above) assumed an unlabeled column in
  a person's row was probably their address. When a column's header was
  lost to a bbox-alignment quirk between the header row and data rows (not
  a deliberate absence), this fired on an unrelated column (a Designation
  cell got redacted as an address). Fixed by additionally requiring the
  *value itself* to look address-shaped (contains a digit, plus a comma or
  4+ words) before trusting the inherited context — an explicitly
  address-labeled field is still trusted on label alone.
- **Font size and multi-line bboxes**: the replacement text now matches the
  original word's actual font size (via pdfplumber's `size` attribute)
  instead of a fixed size, so a redacted name doesn't look visually out of
  place next to surrounding body text. When the original value wraps across
  two display lines, the tool blanks the full multi-line area but inserts
  the (usually shorter) replacement text sized to a single line — otherwise
  PyMuPDF stretches a one-line replacement to fill the full multi-line box.

After these fixes, the same director-table names and addresses that appear
in the DOCX version produce **identical fake values** in the PDF version
(confirming the deterministic consistency mapping works across formats),
and a rendered visual check of the redacted pages showed no corruption or
overlapping text.

## Setup

```
pip install -r requirements.txt
python -m spacy download en_core_web_lg   # one-time setup, not a runtime call
```

Run the evaluation suite: `python evaluate.py` → writes `eval_report.md`.
