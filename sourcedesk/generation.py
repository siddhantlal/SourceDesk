"""Grounded structured generation; citation structure is not proof of semantic support."""
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sourcedesk.chunking import token_count
from sourcedesk.providers import GenerationProvider, ProviderError

PROMPT_VERSION = "grounded-v1"
INSUFFICIENT = "Insufficient evidence in the selected documents."


class CitationValidationError(ValueError):
    pass


class CitationRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(max_length=12)
    quote: str | None = Field(default=None, max_length=16000)


class StructuredAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["answered", "insufficient_evidence"]
    answer: str = Field(min_length=1, max_length=16000)
    citations: list[CitationRef] = Field(max_length=20)


@dataclass
class AnswerResult:
    status: str
    text: str
    citations: list[dict] = field(default_factory=list)
    model: str = "unknown"
    token_usage: dict | None = None
    warnings: list[str] = field(default_factory=list)


def validate_response(payload: dict, sources: list[dict], *, max_output_tokens: int = 1000) -> tuple[str, str, list[dict]]:
    try:
        parsed = StructuredAnswer.model_validate(payload)
    except ValidationError as exc:
        raise CitationValidationError("Invalid structured answer.") from exc
    if token_count(parsed.answer) > max_output_tokens:
        raise CitationValidationError("Answer exceeds token limit.")
    by_id = {s["source_id"]: s for s in sources}
    if parsed.status == "insufficient_evidence":
        if parsed.citations:
            raise CitationValidationError("Abstentions cannot claim citations.")
        return parsed.status, INSUFFICIENT, []
    if not parsed.citations:
        raise CitationValidationError("Answered responses require citations.")
    citations = []
    seen = set()
    for ref in parsed.citations:
        if ref.source_id not in by_id:
            raise CitationValidationError("Unknown citation identifier.")
        source = by_id[ref.source_id]
        if ref.quote is not None and (not ref.quote.strip() or ref.quote not in source["excerpt"]):
            raise CitationValidationError("Quoted excerpt is not in supplied source.")
        if ref.source_id not in seen:
            citations.append({key: source[key] for key in
                              ("source_id", "document_id", "chunk_id", "filename", "page_number", "excerpt")})
            if ref.quote:
                citations[-1]["quote"] = ref.quote
            seen.add(ref.source_id)
    return parsed.status, parsed.answer, citations


def generate_answer(question: str, sources: list[dict], provider: GenerationProvider, *,
                    max_output_tokens: int = 1000) -> AnswerResult:
    if not sources:
        return AnswerResult("insufficient_evidence", INSUFFICIENT, model=provider.model,
                            warnings=["No eligible indexed passages were found."])
    repair = None
    usage: dict[str, int] = {}
    for attempt in range(2):
        try:
            response = provider.generate(question, sources, max_output_tokens=max_output_tokens, repair=repair)
            if response.token_usage:
                for key, value in response.token_usage.items():
                    if isinstance(value, int):
                        usage[key] = usage.get(key, 0) + value
            try:
                status, text, citations = validate_response(response.payload, sources,
                                                            max_output_tokens=max_output_tokens)
                return AnswerResult(status, text, citations, response.model, usage or None, response.warnings)
            except CitationValidationError:
                repair = "Previous response failed structural validation. Use only supplied source IDs and exact quotes."
        except ProviderError as exc:
            return AnswerResult("provider_unavailable", "The model provider is unavailable; retry later.",
                                model=provider.model, token_usage=usage or None, warnings=[exc.code])
    return AnswerResult("generation_error", "The generated answer failed citation validation.",
                        model=provider.model, token_usage=usage or None,
                        warnings=["Structural validation failed after one repair attempt."])
