"""Read-only image integrity verification."""
from services.physical_memory_backend import verify_image

def verify(path, **kwargs): return verify_image(path, **kwargs)
