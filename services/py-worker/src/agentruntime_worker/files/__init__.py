"""Sandbox-local file engines. Runtime owns identities, grants and publication."""

def process(request):
    from .cli import process as run
    return run(request)
