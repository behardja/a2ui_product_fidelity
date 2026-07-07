"""A2UI Product-Fidelity agent.

The LLM orchestrates: browse/ingest reference images from GCS, run the Gecko
fidelity-eval loop, and render everything as A2UI JSON. Tools return raw JSON;
the model (grounded by the A2UI schema + few-shot examples) turns it into UI
wrapped in <a2ui-json> tags, which the executor extracts into A2UI DataParts.
"""

import os
import logging

from google.adk.agents import Agent
from google.adk.tools import FunctionTool, load_artifacts
from google.genai import types as genai_types
from a2ui.schema.manager import A2uiSchemaManager
from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.common_modifiers import remove_strict_validation
from a2ui.schema.constants import VERSION_0_9

logger = logging.getLogger(__name__)

try:
    from .tools import (
        list_gcs_images,
        ingest_uploaded_image_tool,
        run_fidelity_eval,
        get_eval_defaults,
    )
except ImportError:
    from tools import (
        list_gcs_images,
        ingest_uploaded_image_tool,
        run_fidelity_eval,
        get_eval_defaults,
    )


ROLE_DESCRIPTION = (
    "You are a product catalog fidelity assistant. You help users select a "
    "product reference image from Google Cloud Storage (or an uploaded image), "
    "run an automated fidelity-evaluation loop that generates a candidate asset "
    "and scores how faithfully it matches the reference, and present the results "
    "as rich interactive A2UI cards and charts."
)

WORKFLOW_DESCRIPTION = """
Choose the workflow based on the user's intent:

A. BROWSE GCS IMAGES
   1. When the user asks to browse/list images in a GCS bucket or prefix, call
      `list_gcs_images` with their gs:// prefix.
   2. Render the returned images as a selectable grid: one Image per item plus a
      Button whose action name is "select_reference" and whose context carries
      the image's `gs_uri` (key "referenceUri") and `name`.

B. EVALUATE A REFERENCE
   1. If the user uploaded an image, first call `ingest_uploaded_image_tool` to
      store it and get its gs_uri.
   2. If the user selected/pasted a gs:// URI, use that directly.
   3. Call `run_fidelity_eval` with reference_uris=[the gs_uri] (and optional
      user_prompt, threshold, max_retries, image_model when specified — pass any
      `image_model` string from the request through verbatim).
   4. Parse the returned JSON and render the results UI (see UI rules): the
      reference image, the best candidate image, a score chart across attempts,
      a pass/fail status, and the passing/failing rubric verdicts.

C. ADJUST SETTINGS BEFORE EVALUATING
   1. When the user wants to tune the run (or asks for settings/options), first
      call `get_eval_defaults`, then render the "Evaluation settings" panel
      (see UI rules) pre-filled with those defaults and the chosen referenceUri.
   2. When the user clicks the "run_eval" action, read referenceUri, threshold,
      maxRetries, and userPrompt from the action context and call
      `run_fidelity_eval` with them, then render the results UI (workflow B.4).

Keep prose to at most ONE short sentence (e.g. "Here are the images." or
"Evaluation complete."), then emit the A2UI UI block. Do NOT restate scores,
verdicts, or a written evaluation report in prose — ALL results belong in the
A2UI widgets (images, score list, verdict lists), not the text.
When a user clicks a "select_reference" action, treat the provided referenceUri
as the reference and proceed with workflow B (or C if they asked to adjust settings).
"""

UI_DESCRIPTION = """
Emit A2UI **v0.9**: an array of messages, each tagged `"version": "v0.9"`, using
`createSurface` (with a `catalogId` of
"https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json" AND a
`"theme": {"primaryColor": "#135bec"}` so widgets match the app's brand color),
then `updateComponents`, then `updateDataModel`. Components are FLAT objects like
`{"id","component":"Column","children":["a","b"]}`; the root component's id is
"root". Bind values with `{"path":"field"}` (relative inside a List item, absolute
like `/threshold` otherwise). Follow the provided few-shot examples closely.

- GCS image browser: a `Column` → `List` whose `children` is
  `{"componentId":"image-card","path":"/images"}`; each item is a `Card`/`Row` with
  an `Image` (`"url": {"path":"url"}`, `"fit":"cover"`, `"variant":"smallFeature"`),
  a `Text` name, and a `Button` whose child Text reads **"Generate and Evaluate"**
  (`"action":{"event":{"name":"select_reference","context":{"referenceUri":{"path":"gs_uri"},"name":{"path":"name"}}}}`).
  Put the images (name, gs_uri, url) in `updateDataModel` under `/images`.
- Fidelity results — title it **"Fidelity Report"** (Text variant "h2"), then:
  * A status `Text` (variant "h3") like "✅ PASS · Score 0.82 · 3 attempts" (use ❌
    and "FAIL" when not passed; ALWAYS include the number of attempts).
  * A `Row` of two `Card`s: reference `Image` (left) and best-candidate `Image`
    (right, the highest-scoring attempt's candidate_url), each `Image` with
    `"fit":"contain"` and `"variant":"largeFeature"` (prominent), plus a `Text` caption.
  * A `Tabs` component grouping the details:
    `"tabs":[{"title":"Passing (N)","child":"passing-list"},{"title":"Failing (M)","child":"failing-list"},{"title":"Scores","child":"scores-list"}]`.
    - passing-list / failing-list: `List` bound to `/passing` / `/failing`; each item
      is a `Row` of two `Text`s — a mark ("✅" for passing, "❌" for failing) then the
      verdict bound to `{"path":"text"}`.
    - scores-list: `List` bound to `/attempts`; each item a `Row` of two `Text`s —
      the attempt label and its score (format the score as text, e.g. "0.82").
      (There is NO chart component in v0.9.)
- Evaluation settings panel (a `Card` → `Column`):
  * `TextField` `"value":{"path":"/referenceUri"}` (the gs:// reference).
  * `Slider` `"value":{"path":"/threshold"}`, `"min":0`, `"max":1`.
  * `Slider` `"value":{"path":"/maxRetries"}`, `"min":1`, `"max":5`.
  * `TextField` `"value":{"path":"/userPrompt"}` for creative direction.
  * `Button` `"action":{"event":{"name":"run_eval","context":{"referenceUri":{"path":"/referenceUri"},"threshold":{"path":"/threshold"},"maxRetries":{"path":"/maxRetries"},"userPrompt":{"path":"/userPrompt"}}}}`.
  Pre-fill `updateDataModel` with `get_eval_defaults` values + the referenceUri.
- Image components MUST use the signed `url` fields returned by the tools (not
  gs:// URIs) so they can be displayed.
- Do NOT put markdown syntax (##, **, -, etc.) inside `Text` values; write plain
  words only and use the `variant` property ("h2","h3","h4","caption","body") for
  size/emphasis. (e.g. text "Select a reference image" with variant "h2" — never
  "## Select a reference image".)
- Size `Image` with `variant`: grid/browser thumbnails use "smallFeature";
  reference vs candidate result images use "largeFeature".
- The fidelity report (scores, pass/fail, verdicts) MUST be rendered as the A2UI
  widgets above — never written out as prose text.
- ALL UI MUST be wrapped in `<a2ui-json>` and `</a2ui-json>` tags. DO NOT output
  raw JSON without these tags.
"""


def create_agent() -> Agent:
    schema_manager = A2uiSchemaManager(
        version=VERSION_0_9,
        catalogs=[
            BasicCatalog.get_config(
                version=VERSION_0_9,
                examples_path=os.path.join(os.path.dirname(__file__), "examples/0.9"),
            )
        ],
        schema_modifiers=[remove_strict_validation],
    )

    instruction = schema_manager.generate_system_prompt(
        role_description=ROLE_DESCRIPTION,
        workflow_description=WORKFLOW_DESCRIPTION,
        ui_description=UI_DESCRIPTION,
        include_schema=True,
        include_examples=True,
        validate_examples=False,
    )

    return Agent(
        name="ProductFidelityAgent",
        model=os.environ.get("GOOGLE_GENAI_MODEL", "gemini-3.5-flash"),
        description=(
            "Product catalog fidelity agent: browse GCS references, run the "
            "Gecko eval loop, render results as A2UI."
        ),
        # Pass the instruction as a callable (InstructionProvider) so ADK skips
        # {var} state-injection — the embedded v0.9 A2UI schema contains literal
        # braces like `{expression}` that would otherwise raise KeyError.
        instruction=lambda _ctx: instruction,
        # A2UI grids/results embed long signed URLs verbatim; give the model
        # ample output budget so it never truncates mid-<a2ui-json> block.
        generate_content_config=genai_types.GenerateContentConfig(
            max_output_tokens=int(os.environ.get("MAX_OUTPUT_TOKENS", "16384")),
        ),
        tools=[
            load_artifacts,
            FunctionTool(list_gcs_images),
            FunctionTool(ingest_uploaded_image_tool),
            FunctionTool(get_eval_defaults),
            FunctionTool(run_fidelity_eval),
        ],
    )


_root_agent = None


def get_agent() -> Agent:
    global _root_agent
    if _root_agent is None:
        _root_agent = create_agent()
    return _root_agent


root_agent = get_agent()
