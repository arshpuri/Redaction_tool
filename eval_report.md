# Evaluation Report

Hand-labeled fixtures across all four formats (JSON, DOCX, PDF, TXT), run through the same detection core used by `redact.py`.

## Overall

| Metric | Value |
|---|---|
| True Positives | 18 |
| False Negatives | 0 |
| False Positives | 0 |
| True Negatives (correctly left alone) | 11 |
| Precision | 1.00 |
| Recall | 1.00 |
| F1 | 1.00 |

## JSON fixture (`ticket.json`)

| Field / value | Expected | Detected | Result |
|---|---|---|---|
| `ticket_id` | _(none)_ | _(none)_ | TN |
| `customer_name` | name | name | TP |
| `contact.email` | email | email | TP |
| `contact.phone` | phone | phone | TP |
| `notes` | name | name | TP |
| `billing_address` | address | address | TP |
| `din` | _(none)_ | _(none)_ | TN |
| `plan_id` | _(none)_ | _(none)_ | TN |

## TXT fixture (`sample.txt`)

| Field / value | Expected | Detected | Result |
|---|---|---|---|
| `line[0]` | name | name | TP |
| `line[1]` | email | email | TP |
| `line[2]` | phone | phone | TP |
| `line[3]` | _(none)_ | _(none)_ | TN |
| `line[4]` | _(none)_ | _(none)_ | TN |
| `line[5]` | address | address | TP |
| `line[6]` | dob | dob | TP |
| `line[8]` | name | name | TP |
| `line[9]` | _(none)_ | _(none)_ | TN |

## DOCX fixture (`board.docx`)

| Field / value | Expected | Detected | Result |
|---|---|---|---|
| `body.paragraph[0]` | phone | email, phone | TP |
| `body.paragraph[0]` | email | email, phone | TP |
| `body.paragraph[1]` | _(none)_ | _(none)_ | TN |
| `table[0].row[1].cell[0]` | name | name | TP |
| `table[0].row[1].cell[3]` | address | address | TP |
| `table[0].row[1].cell[2]` | _(none)_ | _(none)_ | TN |
| `table[0].row[2].cell[0]` | _(none)_ | _(none)_ | TN |
| `table[0].row[2].cell[3]` | _(none)_ | _(none)_ | TN |

## PDF fixture (`sample.pdf`)

| Field / value | Expected | Detected | Result |
|---|---|---|---|
| `Priya Nair` | name | name | TP |
| `priya.nair@examplecorp.com` | email | email | TP |
| `9876501234` | phone | phone | TP |
| `5567123` | _(none)_ | _(none)_ | TN |

## Notes on observed failure modes

- Name detection intentionally avoids bare spaCy PERSON hits (weak recall on Indian names) in favor of context+shape and honorific/kinship patterns; recall on names with neither an explicit label nor an honorific/kinship marker nearby is a known gap.
- Free-flowing prose addresses with no label or table-column context are out of scope for the balanced tier — only labeled/columnar address fields are redacted.
- The PDF fixture is scored by value substring rather than exact node path, since pdfplumber's line-clustering order isn't guaranteed stable across inputs.
