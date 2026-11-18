"""Modern macOS full-RAM acquisition is unsupported without a verified adapter."""
from services.physical_memory_backend import UnsupportedCapture

def status(): return {'name':'macOS physical RAM','ready':False,'compatible':False,'reason':'No validated macOS physical RAM backend configured'}
def acquire(*args, **kwargs): raise UnsupportedCapture(status()['reason'])
