"""Safe, cross-platform system metadata."""
import getpass
import platform
import socket
import psutil
from .architecture_detector import detect_architecture
from .privilege_manager import privilege_status

def detect_system():
    memory=psutil.virtual_memory()
    return {"os":platform.system(),"version":platform.version(),"release":platform.release(),"hostname":socket.gethostname(),"username":getpass.getuser(),"architecture":detect_architecture(),"privileges":privilege_status(),"ram_total":memory.total,"ram_available":memory.available,"boot_time":psutil.boot_time()}
