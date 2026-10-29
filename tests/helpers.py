import re


def U(text: str) -> str:
    """Strip the <untrusted_page_content> wrapper from a tool result."""
    m = re.fullmatch(r"<untrusted_page_content>\n(.*)\n</untrusted_page_content>", text, re.S)
    return m.group(1) if m else text
