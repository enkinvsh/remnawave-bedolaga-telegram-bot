from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    html: str
    text: str | None
