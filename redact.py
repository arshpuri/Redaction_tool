#!/usr/bin/env python3
"""CLI entry point: detect format -> parse -> structure-aware redact -> write.

Usage:
    python redact.py <input_path> [-o output_path] [--on-conflict ask|redact|skip] [--force]
"""

import argparse
import os
import sys

import fake_data
import parsers
from detectors import detect_leaf, merge_span

PARSE_FN = {
    "json": parsers.parse_json,
    "docx": parsers.parse_docx,
    "pdf": parsers.parse_pdf,
    "txt": parsers.parse_txt,
}
WRITE_FN = {
    "json": parsers.write_json,
    "docx": parsers.write_docx,
    "pdf": parsers.write_pdf,
    "txt": parsers.write_txt,
}


def _redact_table_row(row_node):
    """Two-pass detection for a table row: if any cell resolves to a person's
    name, sibling cells with no header label of their own (e.g. an unlabeled
    column) inherit 'personal capacity' context for the address rule."""
    results = {}
    has_person = False
    for cell in row_node.children:
        spans, conflicts = detect_leaf(cell, row_context=None)
        results[cell.id] = (spans, conflicts)
        if any(s.pii_type == "name" for s in spans):
            has_person = True
    if has_person:
        for cell in row_node.children:
            spans, conflicts = results[cell.id]
            if not spans and cell.label is None:
                results[cell.id] = detect_leaf(cell, row_context={"has_person": True})
    return results


def _detect_with_pseudo_fields(node):
    """Detect on a leaf, folding in any pseudo_field children's detections.

    A DOCX paragraph / PDF line can contain embedded "Label: value" segments
    (e.g. "Telephone: 022-... Email: x@y.com"). Those get their own labeled,
    atomic pseudo_field child node so context-dependent rules (dob, address,
    name-shape) and label/value conflict checks can fire on them. But the
    child's span offsets are local to its own value substring — they're
    translated back into the parent's coordinate space and merged into the
    SAME span list here, so write-back applies exactly one consistent set of
    offsets to the underlying paragraph instead of two passes that would
    invalidate each other's offsets after the first splice.
    """
    spans, conflicts = detect_leaf(node)
    for child in node.children:
        if child.node_type != "pseudo_field":
            continue
        child_spans, child_conflicts = detect_leaf(child)
        base = child.meta["span_in_parent"][0]
        for sp in child_spans:
            merge_span(spans, sp.start + base, sp.end + base, sp.pii_type, sp.reason, node.value)
        conflicts.extend(child_conflicts)
    return spans, conflicts


def _walk_and_detect(node, leaf_spans_out, conflicts_out):
    if node.node_type == "table_row":
        row_results = _redact_table_row(node)
        for cell in node.children:
            spans, conflicts = row_results.get(cell.id, ([], []))
            if spans:
                leaf_spans_out.append((cell, spans))
            conflicts_out.extend(conflicts)
        return
    if node.is_leaf():
        spans, conflicts = _detect_with_pseudo_fields(node)
        if spans:
            leaf_spans_out.append((node, spans))
        conflicts_out.extend(conflicts)
        # pseudo_field children were already folded into `spans` above (their
        # offsets translated into this node's own coordinate space); only
        # recurse into any other children so they aren't detected twice.
        for child in node.children:
            if child.node_type != "pseudo_field":
                _walk_and_detect(child, leaf_spans_out, conflicts_out)
        return
    # TXT's pseudo_field nodes are top-level (no leaf parent to fold them
    # into), so they fall through to here and get detected normally.
    for child in node.children:
        _walk_and_detect(child, leaf_spans_out, conflicts_out)


def redact_document(root):
    leaf_spans, conflicts = [], []
    _walk_and_detect(root, leaf_spans, conflicts)
    return leaf_spans, conflicts


def apply_fakes(leaf_spans):
    counts = {}
    examples = []
    for node, spans in leaf_spans:
        redactions = []
        for sp in spans:
            fake = fake_data.fake_for(sp.pii_type, sp.text)
            redactions.append((sp.start, sp.end, fake))
            counts[sp.pii_type] = counts.get(sp.pii_type, 0) + 1
            examples.append((sp.pii_type, sp.text, fake, node.path))
        node.meta["redactions"] = redactions
    return counts, examples


def main():
    ap = argparse.ArgumentParser(description="Structure-aware local PII redaction tool")
    ap.add_argument("input_path")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--on-conflict", choices=["ask", "redact", "skip"], default="ask",
                     help="what to do when a label strongly implies a type but the "
                          "value doesn't match (default: ask, i.e. stop and report)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing output file")
    args = ap.parse_args()

    try:
        fmt = parsers.detect_format(args.input_path)
    except parsers.FormatMismatch as e:
        print(f"STOP: {e}")
        print("The file extension and its actual content disagree — "
              "please confirm the real format before proceeding.")
        sys.exit(1)
    except FileNotFoundError:
        print(f"STOP: no such file: {args.input_path}")
        sys.exit(1)

    base, ext = os.path.splitext(args.input_path)
    output_path = args.output or f"{base}_redacted{ext}"

    if os.path.exists(output_path) and not args.force:
        print(f"STOP: output file already exists: {output_path}")
        print("Re-run with --force to overwrite, or pass -o to choose a different path.")
        sys.exit(1)

    root, handle = PARSE_FN[fmt](args.input_path)
    leaf_spans, conflicts = redact_document(root)

    if conflicts and args.on_conflict == "ask":
        print(f"STOP: {len(conflicts)} label/value conflict(s) found — refusing to guess.")
        for c in conflicts[:20]:
            print(f"  - [{c.pii_type}] {c.node.path}: {c.message} (value={c.node.value!r})")
        if len(conflicts) > 20:
            print(f"  ... and {len(conflicts) - 20} more")
        print()
        print("Re-run with --on-conflict=redact to trust the label and redact anyway,")
        print("or --on-conflict=skip to leave these specific fields untouched.")
        sys.exit(1)

    counts, examples = apply_fakes(leaf_spans)

    if conflicts:
        verb = "Redacting anyway" if args.on_conflict == "redact" else "Leaving untouched"
        print(f"NOTE: {len(conflicts)} label/value conflict(s) found ({verb}, --on-conflict={args.on_conflict}):")
        for c in conflicts[:10]:
            print(f"  - [{c.pii_type}] {c.node.path}: {c.message} (value={c.node.value!r})")
        if len(conflicts) > 10:
            print(f"  ... and {len(conflicts) - 10} more")
        print()

    if conflicts and args.on_conflict == "redact":
        for c in conflicts:
            n = c.node
            fake = fake_data.fake_for(c.pii_type, n.value)
            n.meta.setdefault("redactions", []).append((0, len(n.value), fake))
            counts[c.pii_type] = counts.get(c.pii_type, 0) + 1
            examples.append((c.pii_type, n.value, fake, n.path))

    WRITE_FN[fmt](root, handle, output_path)

    print(f"Format detected: {fmt}")
    print(f"Output written to: {output_path}")
    print()
    if not counts:
        print("No PII detected.")
        return
    print("Redactions by type:")
    for t, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {t:10s} {n}")
    print()
    print("Examples:")
    for pii_type, before, after, path in examples[:3]:
        print(f"  [{pii_type}] {path}")
        print(f"    before: {before!r}")
        print(f"    after:  {after!r}")


if __name__ == "__main__":
    main()
