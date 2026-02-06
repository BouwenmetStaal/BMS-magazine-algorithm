from __future__ import annotations
from enum import Enum

class TextType(Enum):
    """Enum for different types of text content in the magazine."""
    INTRO = "intro"
    SUBHEADING = "subheading"
    PARAGRAPH = "paragraph"
    BODY = "body"