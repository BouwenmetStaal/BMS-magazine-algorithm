from __future__ import annotations
from enum import Enum

class TextType(Enum):
    """Enum for different types of text content in the magazine."""
    INTRO = "intro"
    SUBHEADING = "subheading"
    PARAGRAPH = "paragraph"
    BODY = "body"

class TOClineType(Enum):
    """Enum for different types of TOC lines."""
    CHAPOT = "chapot"
    TITLE = "title"
    AUTHOR = "author"
    OTHER = "other"