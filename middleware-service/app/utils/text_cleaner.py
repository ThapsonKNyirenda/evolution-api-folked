def clean_surrogates(text: str) -> str:
    if not isinstance(text, str):
        return text
    try:
        return text.encode('utf-8', errors='replace').decode('utf-8')
    except Exception:
        return text
