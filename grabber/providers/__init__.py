from grabber.providers.docsend import DocsendProvider

PROVIDERS = {
    "docsend": DocsendProvider,
}


def detect_provider(url: str):
    """Return the appropriate provider class for a given URL."""
    for name, cls in PROVIDERS.items():
        if cls.can_handle(url):
            return cls
    return None
