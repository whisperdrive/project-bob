"""Sign in to Entra (device code, cached) and list the Foundry project's model deployments."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))
from azure.ai.projects import AIProjectClient  # noqa: E402
from llm import PROJECT_ENDPOINT, credential  # noqa: E402

project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=credential())
for d in project.deployments.list():
    print(d.as_dict(), flush=True)
