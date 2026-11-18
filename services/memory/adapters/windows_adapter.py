"""Windows acquisition adapter: only verified WinPmem x64 is eligible."""
from services.physical_memory_backend import detect_backends, capture_memory

def status(): return next(t for t in detect_backends() if t['name']=='WinPmem')
def acquire(executable, output, **kwargs): return capture_memory('WinPmem',executable,output,**kwargs)
