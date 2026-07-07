"""DEFERRED — do not run yet.

Deploys the product-fidelity agent to Vertex AI Agent Engine and registers it
on Gemini Enterprise (Discovery Engine), injecting the A2UI v0.9 extension into
the agent card. Adapted from the in-workspace invoice-auditor sample.

Prereqs (set in .env before running later):
  PROJECT_ID, LOCATION, STORAGE_BUCKET (gs://), GEMINI_ENTERPRISE_APP_ID,
  AGENT_AUTHORIZATION, plus the model/bucket vars used at runtime.
Run (later):  python deploy.py
"""

import json
import os

from a2a.types import AgentSkill
from dotenv import load_dotenv
from google.auth import default
from google.auth.transport.requests import Request
from google.genai import types
import httpx
import requests
import vertexai
from vertexai.preview.reasoning_engines import A2aAgent
from vertexai.preview.reasoning_engines.templates.a2a import create_agent_card

from executor import ProductFidelityExecutor

A2UI_EXTENSION_URI = "https://a2ui.org/a2a-extension/a2ui/v0.9"
A2UI_CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"


def _get_bearer_token():
    try:
        credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(Request())
        return credentials.token
    except Exception as e:  # pylint: disable=broad-except
        print(f"Error getting credentials: {e}")
        print("Run 'gcloud auth application-default login' first.")
    return None


def _register_agent_on_gemini_enterprise(
    project_id, app_id, agent_card, agent_name, display_name, description,
    agent_authorization=None,
):
    api_endpoint = (
        f"https://discoveryengine.googleapis.com/v1alpha/projects/{project_id}/"
        f"locations/global/collections/default_collection/engines/{app_id}/"
        "assistants/default_assistant/agents"
    )
    payload = {
        "name": agent_name,
        "displayName": display_name,
        "description": description,
        "a2aAgentDefinition": {"jsonAgentCard": agent_card},
    }
    if agent_authorization:
        payload["authorization_config"] = {"agent_authorization": agent_authorization}

    bearer_token = _get_bearer_token()
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "X-Goog-User-Project": project_id,
    }
    response = requests.post(api_endpoint, headers=headers, json=payload)
    if response.status_code == 200:
        print("✓ Agent registered successfully!")
        return response.json()
    print(f"✗ Registration failed: {response.status_code}\n{response.text}")
    response.raise_for_status()


def main():
    load_dotenv()
    project_id = os.environ.get("PROJECT_ID")
    location = os.environ.get("LOCATION")
    storage = os.environ.get("STORAGE_BUCKET")
    app_id = os.environ.get("GEMINI_ENTERPRISE_APP_ID")
    api_endpoint = f"{location}-aiplatform.googleapis.com"

    vertexai.init(project=project_id, location=location,
                  api_endpoint=api_endpoint, staging_bucket=storage)
    client = vertexai.Client(
        project=project_id, location=location,
        http_options=types.HttpOptions(api_version="v1beta1"),
    )

    skill = AgentSkill(
        id="product-fidelity-eval",
        name="Product Fidelity Evaluation",
        description=(
            "Browse GCS product images, generate a candidate with "
            "gemini-3.1-flash-image, score fidelity with Gecko, render as A2UI."
        ),
        tags=["product", "fidelity", "eval", "image", "chart"],
        examples=[
            "List images in gs://my-bucket/products/",
            "Evaluate gs://my-bucket/products/sneaker.png",
        ],
    )
    agent_card = create_agent_card(
        agent_name="Product Fidelity Agent",
        description="Evaluates product-image fidelity and displays interactive A2UI results.",
        skills=[skill],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )

    a2ui_agent = A2aAgent(
        agent_card=agent_card,
        agent_executor_builder=ProductFidelityExecutor,
    )
    a2ui_agent.set_up()

    config = {
        "display_name": "product_fidelity_a2ui",
        "description": "A2UI product-fidelity agent (Gecko eval + Nano Banana 2).",
        "agent_framework": "google-adk",
        "staging_bucket": storage,
        "gcs_dir_name": "product_fidelity_a2ui",
        "requirements": [
            "google-cloud-aiplatform[agent_engines,adk]==1.148.0",
            "google-genai==1.73.1",
            "python-dotenv==1.2.2",
            "uvicorn==0.44.0",
            "a2a-sdk==0.3.26",
            "cloudpickle==3.1.2",
            "pydantic==2.13.1",
            "jsonschema==4.26.0",
            "a2ui-agent-sdk==0.2.1",
            "fastapi==0.136.0",
            "pandas",
            "Pillow",
            "google-cloud-storage",
        ],
        "http_options": {"api_version": "v1beta1"},
        "max_instances": 1,
        "extra_packages": [
            "agent.py",
            "executor.py",
            "tools.py",
            "generate.py",
            "examples",
            "evaluation_wrapper",
        ],
        "env_vars": {
            "NUM_WORKERS": "1",
            "GOOGLE_GENAI_USE_VERTEXAI": "true",
            "PROJECT_ID": project_id,
            "LOCATION": location,
            "GOOGLE_CLOUD_LOCATION": location,
            "CANDIDATE_BUCKET": os.environ.get("CANDIDATE_BUCKET", ""),
            "IMAGE_MODEL": os.environ.get("IMAGE_MODEL", "gemini-3.1-flash-image"),
            "GENERATION_LOCATION": os.environ.get("GENERATION_LOCATION", "global"),
            "DESCRIPTION_MODEL": os.environ.get("DESCRIPTION_MODEL", "gemini-3.5-flash"),
            "GOOGLE_GENAI_MODEL": os.environ.get("GOOGLE_GENAI_MODEL", "gemini-3.5-flash"),
            "PASSING_THRESHOLD": os.environ.get("PASSING_THRESHOLD", "0.7"),
            "MAX_RETRIES": os.environ.get("MAX_RETRIES", "3"),
        },
    }

    remote_agent = client.agent_engines.create(agent=a2ui_agent, config=config)
    remote_engine_resource = remote_agent.api_resource.name
    print(f"✓ Remote agent created: {remote_engine_resource}")

    a2a_endpoint = f"https://{api_endpoint}/v1beta1/{remote_engine_resource}/a2a/v1/card"
    headers = {"Authorization": f"Bearer {_get_bearer_token()}", "Content-Type": "application/json"}
    response = httpx.get(a2a_endpoint, headers=headers)
    response.raise_for_status()
    card_json = response.json()

    card_json["capabilities"] = {
        "streaming": False,
        "extensions": [{
            "uri": A2UI_EXTENSION_URI,
            "description": "Ability to render A2UI",
            "required": False,
            "params": {"supportedCatalogIds": [A2UI_CATALOG_ID]},
        }],
    }

    _register_agent_on_gemini_enterprise(
        project_id=project_id,
        app_id=app_id,
        agent_card=json.dumps(card_json),
        agent_name="product_fidelity_a2ui",
        display_name="product_fidelity_a2ui",
        description="A2UI product-fidelity agent with Gecko evaluation.",
        agent_authorization=os.environ.get("AGENT_AUTHORIZATION"),
    )


if __name__ == "__main__":
    main()
