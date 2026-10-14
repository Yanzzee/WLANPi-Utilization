"""Normalize the exported SSID once, before shared analysis and rendering."""
import re
import unicodedata


def normalize_tshark_ssid(value: str, *, encoding: str = 'text') -> str | None:
    """Decode FT_BYTES exports; preserve explicit text inputs without guessing.

    SSIDs are byte sequences. UTF-8 text is readable; other bytes retain a
    lossless, labeled hex representation rather than replacement characters.
    """
    if encoding not in {'text', 'hex'}:
        raise ValueError('SSID encoding must be text or hex')
    if value == '':
        return None
    if encoding == 'text':
        return value
    if value == '<MISSING>':
        return None
    if re.fullmatch(r'(?:[0-9a-fA-F]{2})+', value):
        data = bytes.fromhex(value)
    elif re.fullmatch(r'[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2})+', value):
        data = bytes.fromhex(value.replace(':', ''))
    else:
        # Existing plaintext TSV samples and unknown decoder markers remain
        # readable. A valid hex export is interpreted only in explicit hex mode.
        return value
    if not any(data):
        return None
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return f'hex:{data.hex()}'
    if any(unicodedata.category(char) in {'Cc', 'Cs', 'Zl', 'Zp'} for char in text):
        return f'hex:{data.hex()}'
    return text
