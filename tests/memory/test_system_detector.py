from services.memory.system_detector import detect_system
from services.memory.architecture_detector import detect_architecture
def test_system():
 assert detect_system()["ram_total"] > 0
 assert detect_architecture()["native"]
