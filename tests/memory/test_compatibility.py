from services.memory.compatibility_checker import check_compatibility
def test_status():
 for item in check_compatibility():
  assert item["can_acquire"] == bool(item["ready"] and item["compatible"])
  if not item["can_acquire"]: assert item["disabled_reason"]
