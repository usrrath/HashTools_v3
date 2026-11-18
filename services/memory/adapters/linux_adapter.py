"""Linux acquisition adapter: AVML availability is checked at runtime."""
from services.physical_memory_backend import detect_backends, capture_memory

def status(): return next(t for t in detect_backends() if t['name']=='AVML')
def acquire(executable, output, **kwargs): return capture_memory('AVML',executable,output,**kwargs)
