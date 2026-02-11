from dataclasses import dataclass


@dataclass
class ParagraphText:
    """
    A class to represent the text content of a paragraph in the magazine.
    It includes the paragraph text and its associated type (e.g., intro, subheading, body).
    """
    header: str
    text: str


@dataclass
class ArticleText:
    """
    A class to represent the text content of an article in the magazine.
    """
    intro_text: str
    first_paragraph: str
    paragraph_texts: list[ParagraphText]


    def to_string(self) -> str:
        result = self.intro_text + "\n\n"
        result += self.first_paragraph + "\n\n"
        for paragraph in self.paragraph_texts:
            if paragraph.header:
                result += paragraph.header + "\n"
            result += paragraph.text + "\n\n"
        return result.strip()
