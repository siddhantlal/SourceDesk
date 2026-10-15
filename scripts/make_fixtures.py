"""Rebuild deterministic, self-authored CC0 PDF fixtures and draft question JSONL."""
from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = ROOT / "eval/source/corpus.json"
    corpus = json.loads(source.read_text(encoding="utf-8"))
    out = ROOT / "eval/pdfs"
    out.mkdir(parents=True, exist_ok=True)
    documents = []
    for document in corpus["documents"]:
        path = out / document["filename"]
        canvas = Canvas(str(path), pagesize=A4, invariant=1, pageCompression=1)
        canvas.setTitle(document["title"])
        canvas.setAuthor("SourceDesk self-authored evaluation fixture")
        for number, page in enumerate(document["pages"], 1):
            canvas.setFont("Helvetica-Bold", 15)
            canvas.drawString(48, 790, document["title"])
            canvas.setFont("Helvetica-Bold", 12)
            canvas.drawString(48, 760, page["heading"])
            canvas.setFont("Helvetica", 11)
            for line, text in enumerate(textwrap.wrap(page["text"], width=83)):
                canvas.drawString(48, 730 - line * 17, text)
            canvas.setFont("Helvetica", 9)
            canvas.drawString(48, 35, f"Self-authored fictional fixture | CC0-1.0 | Page {number}")
            canvas.showPage()
        canvas.save()
        reader = PdfReader(path)
        assert len(reader.pages) == len(document["pages"])
        for page, expected in zip(reader.pages, document["pages"], strict=True):
            extracted = " ".join(page.extract_text().split())
            assert expected["text"] in extracted, f"Fixture text mismatch: {path.name}"
        documents.append({"filename": path.name, "pages": len(reader.pages),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "source": "eval/source/corpus.json", "license": "CC0-1.0",
                          "origin": "Self-authored fictional text; not copied from external works"})
    rows = []
    for item in corpus["questions"]:
        rows.append({**item, "collection": corpus["dataset_id"],
                     "review_status": corpus["review_status"],
                     "human_judgments": {"correctness": None, "citation_support": None,
                                         "unsupported_claim_rate": None}})
    (ROOT / "eval/questions.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    provenance = {"dataset_id": corpus["dataset_id"], "license": "CC0-1.0",
                  "review_status": corpus["review_status"],
                  "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                  "generator": "scripts/make_fixtures.py; reportlab Canvas invariant=1",
                  "documents": documents, "questions": len(rows),
                  "splits": {split: sum(r["split"] == split for r in rows)
                             for split in ("dev", "heldout")}}
    (ROOT / "eval/provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(provenance, indent=2))


if __name__ == "__main__":
    main()
