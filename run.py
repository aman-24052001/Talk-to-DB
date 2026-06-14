"""Run Talk-to-DB:  python run.py

First run bootstraps config.yaml from config.example.yaml if it's missing.
TTDB_HOST / TTDB_PORT environment variables override config (used by Docker).
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))          # allow `python run.py` from anywhere

CFG = ROOT / "config.yaml"
EXAMPLE = ROOT / "config.example.yaml"


def main() -> None:
    if not CFG.exists() and EXAMPLE.exists():
        shutil.copy(EXAMPLE, CFG)
        print("• created config.yaml from config.example.yaml — add your Anthropic API key there.")

    import uvicorn
    from app.config import get_config

    cfg = get_config()
    host = os.environ.get("TTDB_HOST", cfg.server.host)
    port = int(os.environ.get("TTDB_PORT", cfg.server.port))
    if not cfg.resolved_api_key:
        print("• no Anthropic API key yet — the UI and /api/schema will work,")
        print("  but /api/ask will return 503 until you set anthropic.api_key")
        print("  in config.yaml or export ANTHROPIC_API_KEY.")
    print(f"\n  Talk-to-DB v3  →  http://{host}:{port}\n")
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
