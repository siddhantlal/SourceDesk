"""Page-bounded cl100k token windows; sentence ends preferred, never inferred pages."""
import re
from dataclasses import dataclass
from functools import lru_cache

import tiktoken

from sourcedesk.ingestion import ExtractedPage


@lru_cache(maxsize=1)
def tokenizer():
    return tiktoken.get_encoding("cl100k_base")


def token_count(text: str) -> int:
    return len(tokenizer().encode(text, disallowed_special=()))


@dataclass(frozen=True)
class TextChunk:
    page_number: int
    position: int
    text: str
    token_count: int


def chunk_pages(pages: list[ExtractedPage], target_tokens: int = 400,
                overlap_tokens: int = 60) -> list[TextChunk]:
    if not 0 <= overlap_tokens < target_tokens:
        raise ValueError("Require 0 <= overlap_tokens < target_tokens")
    enc = tokenizer()
    chunks = []
    for page in pages:
        tokens = enc.encode(page.text, disallowed_special=())
        start = position = 0
        while start < len(tokens):
            end = min(start + target_tokens, len(tokens))
            text = enc.decode(tokens[start:end], errors="ignore")
            if end < len(tokens):
                ends = list(re.finditer(r"[.!?](?:\s|$)", text))
                if ends and ends[-1].end() > len(text) // 2:
                    candidate = text[:ends[-1].end()]
                    size = token_count(candidate)
                    if size > overlap_tokens:
                        end = start + size
                        text = candidate
            text = text.strip()
            if text:
                chunks.append(TextChunk(page.page_number, position, text, token_count(text)))
                position += 1
            if end >= len(tokens):
                break
            start = max(start + 1, end - overlap_tokens)
    return chunks
