"""Pure validation of API keys carried by urllib HTTP headers."""


def is_transport_key(value: object) -> bool:
    """Require printable non-whitespace text that fits urllib header encoding."""
    if (not isinstance(value, str) or not value or not value.isprintable()
            or any(char.isspace() for char in value)):
        return False
    try:
        value.encode('latin-1')
    except UnicodeEncodeError:
        return False
    return True
