"""Memory analysis service with explicit plugin selection."""
import json
from .volatility_adapter import run_plugin

def analyze(image, plugin):
    output=run_plugin(image,plugin)
    try: return json.loads(output)
    except json.JSONDecodeError: return {"raw_output":output,"warning":"Non-JSON Volatility output"}
