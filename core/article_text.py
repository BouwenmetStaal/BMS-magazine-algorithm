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
        text = ""
        text += self.intro_text + "\n\n" if self.intro_text else ""
        text += self.first_paragraph + "\n\n" if self.first_paragraph else ""
        for paragraph in self.paragraph_texts:
            if paragraph.header:
                text += paragraph.header + "\n" if paragraph.header else ""
            text += paragraph.text + "\n\n" if paragraph.text else ""
        return text.strip()
    
    def count_words(self) -> int:
        total_words = 0
        total_words += len(self.intro_text.split())  if self.intro_text else 0
        total_words += len(self.first_paragraph.split()) if self.first_paragraph else 0
        for paragraph in self.paragraph_texts:
            total_words += len(paragraph.header.split()) if paragraph.header else 0
            total_words += len(paragraph.text.split()) if paragraph.text else 0
        return total_words
