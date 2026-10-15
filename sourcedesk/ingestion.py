"""Text-only PDF extraction. Page numbers always come from the PDF parser.

Content streams share a 10 MB decoded-byte budget per page, including reachable
Form XObjects (at most 128 streams, depth 8, and 128 form invocations). Preflight
runs before pypdf assembles /Contents. Only unfiltered, Flate, ASCII85 and
ASCII85 -> Flate text streams are accepted; predictors/other text filters fail
closed. These are practical parser bounds, not a complete PDF sandbox or a
process memory/CPU cap.
"""
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader, apply_configuration
from pypdf.errors import LimitReachedError
from pypdf.generic import ArrayObject, NullObject, StreamObject

PAGE_CONTENT_LIMIT = 10_000_000
PAGE_STREAM_LIMIT = 128
FORM_DEPTH_LIMIT = 8


class ExtractionError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ExtractedPage:
    page_number: int
    text: str
    extraction_metadata: dict


def clean_text(text: str) -> str:
    return "\n".join(re.sub(r"[^\S\n]+", " ", line).strip()
                     for line in text.replace("\x00", "").splitlines()).strip()


def _content_limit():
    return ExtractionError("extraction_limit", "PDF page content exceeds extraction limits.")


def _preflight_content(page):
    remaining = PAGE_CONTENT_LIMIT
    stream_count = 0
    visited: set[int] = set()

    def decode(stream):
        nonlocal remaining, stream_count
        stream_count += 1
        if stream_count > PAGE_STREAM_LIMIT or remaining <= 1 or not isinstance(stream, StreamObject):
            raise _content_limit()
        filters = stream.get("/Filter", [])
        filters = filters.get_object() if hasattr(filters, "get_object") else filters
        filters = list(filters) if isinstance(filters, (list, tuple)) else [filters]
        aliases = {"/Fl": "/FlateDecode", "/A85": "/ASCII85Decode"}
        filters = [aliases.get(str(f), str(f)) for f in filters]
        if filters not in ([], ["/FlateDecode"], ["/ASCII85Decode"],
                           ["/ASCII85Decode", "/FlateDecode"]):
            raise _content_limit()
        params = stream.get("/DecodeParms", [])
        params = params.get_object() if hasattr(params, "get_object") else params
        params = params if isinstance(params, (list, tuple)) else [params]
        if any(isinstance(p.get_object(), dict) and p.get_object().get("/Predictor", 1) != 1
               for p in params if hasattr(p, "get_object")):
            raise _content_limit()
        # pypdf 6.20 caches get_data() and caps zlib via its context-local public
        # Configuration. Its ASCII85 decoder is eager and has no output cap:
        # conservatively bound its maximum expansion BEFORE calling get_data().
        # _data is the encoded bytes used by pypdf.filters.decode_stream_data.
        raw = stream._data
        budget = remaining - 1  # include the separator ContentStream may append
        if len(raw) > budget:
            raise _content_limit()
        if filters and filters[0] == "/ASCII85Decode":
            zero_groups = raw.count(b"z")
            upper_bound = 4 * zero_groups + ((len(raw) - zero_groups + 4) // 5) * 4
            if upper_bound > budget:
                raise _content_limit()
        with apply_configuration(zlib_maximum_output_length=budget):
            decoded = stream.get_data()
        if len(decoded) > budget:
            raise _content_limit()
        remaining -= len(decoded) + 1

    def contents(value):
        if value is None:
            return
        value = value.get_object()
        streams = value if isinstance(value, ArrayObject) else [value]
        if len(streams) > PAGE_STREAM_LIMIT:
            raise _content_limit()
        for stream in streams:
            stream = stream.get_object()
            if not isinstance(stream, NullObject):
                decode(stream)

    def forms(resources, depth):
        if resources is None:
            return
        xobjects = resources.get_object().get("/XObject")
        if xobjects is None:
            return
        xobjects = xobjects.get_object()
        if len(xobjects) > PAGE_STREAM_LIMIT:
            raise _content_limit()
        for reference in xobjects.values():
            form = reference.get_object()
            if form.get("/Subtype") != "/Form" or id(form) in visited:
                continue
            if depth > FORM_DEPTH_LIMIT:
                raise _content_limit()
            visited.add(id(form))
            decode(form)
            forms(form.get("/Resources"), depth + 1)

    contents(page.get("/Contents"))
    forms(page.get("/Resources"), 1)


def extract_pdf(path: Path, max_pages: int = 100) -> list[ExtractedPage]:
    try:
        with apply_configuration(
            maximum_declared_stream_length=PAGE_CONTENT_LIMIT,
            array_based_stream_maximum_output_length=PAGE_CONTENT_LIMIT,
            zlib_maximum_output_length=PAGE_CONTENT_LIMIT,
            lzw_maximum_output_length=PAGE_CONTENT_LIMIT,
            run_length_maximum_output_length=PAGE_CONTENT_LIMIT,
            stream_decoding_work_maximum_length=4 * PAGE_CONTENT_LIMIT,
            xform_maximum_invocations_per_extraction=PAGE_STREAM_LIMIT,
        ):
            return _extract_pdf(path, max_pages)
    except (ExtractionError, LimitReachedError) as exc:
        if isinstance(exc, ExtractionError):
            raise
        raise _content_limit() from exc
    except Exception as exc:
        raise ExtractionError("extraction_failed", "PDF text extraction failed.") from exc


def _extract_pdf(path, max_pages):
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ExtractionError("encrypted_pdf", "Password-protected PDFs are not supported.")
    if not 0 < len(reader.pages) <= max_pages:
        raise ExtractionError("page_limit", "PDF exceeds the permitted page count.")
    pages = []
    for number, page in enumerate(reader.pages, 1):
        _preflight_content(page)
        text = clean_text(page.extract_text() or "")
        if len(text) > 200_000:
            raise ExtractionError("extraction_limit", "PDF page text exceeds extraction limits.")
        pages.append(ExtractedPage(number, text, {"extractor": "pypdf", "text_chars": len(text)}))
    if sum(sum(c.isalnum() for c in page.text) for page in pages) < 20:
        raise ExtractionError("insufficient_text", "Insufficient extractable text; scanned/image-only PDFs require OCR, outside v1.")
    return pages

