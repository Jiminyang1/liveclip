import os
from pathlib import Path

# Must happen before anything imports liveclip (the config is loaded once at import time).
os.environ["LIVECLIP_CONFIG"] = str(Path(__file__).parent / "data" / "config.yaml")
os.environ["LIVECLIP_VISION"] = "none"
os.environ.pop("ANTHROPIC_API_KEY", None)
