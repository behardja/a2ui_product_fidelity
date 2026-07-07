"""Tools for the A2UI product-fidelity agent.

- list_gcs_images:   browse a GCS prefix, return image URIs + signed URLs
- ingest_uploaded_image_tool: persist a user-uploaded image to GCS -> gs:// URI
- run_fidelity_eval: run the full Gecko eval loop via the vendored
  evaluation_wrapper, using gemini-3.1-flash-image for generation.

Tools return plain JSON strings; the LLM turns that JSON into A2UI UI.
"""

import json
import logging
import os
import re
import uuid
from datetime import timedelta

logger = logging.getLogger(__name__)

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif")


# --- GCP helpers ---------------------------------------------------------

def _project() -> str:
    project = os.environ.get("PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise ValueError("Missing PROJECT_ID / GOOGLE_CLOUD_PROJECT env var.")
    return project


def _default_bucket() -> str:
    bucket = os.environ.get("CANDIDATE_BUCKET") or os.environ.get("BUCKET_NAME") or ""
    return bucket.replace("gs://", "").split("/", 1)[0]


def _split_gs(uri: str):
    path = uri.replace("gs://", "", 1)
    parts = path.split("/", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _mime_for(name: str) -> str:
    ext = name.lower().rsplit(".", 1)[-1]
    return "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"


def _signed_url(blob, minutes: int = 60) -> str:
    """Best-effort V4 signed URL so A2UI's Image component can display GCS objects.

    Works directly with service-account key credentials; falls back to IAM
    SignBlob when running under compute/ADC credentials (e.g. a GCP notebook).
    Returns "" if signing is unavailable — the flow still works, the image
    just won't render.
    """
    try:
        return blob.generate_signed_url(
            version="v4", expiration=timedelta(minutes=minutes), method="GET"
        )
    except Exception:
        try:
            import google.auth
            import google.auth.transport.requests

            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            creds.refresh(google.auth.transport.requests.Request())
            email = getattr(creds, "service_account_email", None) or os.environ.get(
                "SIGNING_SA_EMAIL"
            )
            return blob.generate_signed_url(
                version="v4",
                expiration=timedelta(minutes=minutes),
                method="GET",
                service_account_email=email,
                access_token=creds.token,
            )
        except Exception as e:  # pragma: no cover - environment dependent
            logger.warning("Signed URL unavailable for %s: %s", blob.name, e)
            return ""


def _signed_url_for_uri(gs_uri: str) -> str:
    if not gs_uri.startswith("gs://"):
        return ""
    from google.cloud import storage

    bucket_name, blob_name = _split_gs(gs_uri)
    if not bucket_name or not blob_name:
        return ""
    blob = storage.Client(project=_project()).bucket(bucket_name).blob(blob_name)
    return _signed_url(blob)


# --- Tools ---------------------------------------------------------------

def list_gcs_images(gcs_prefix: str, max_results: int = 6) -> str:
    """List product images under a Google Cloud Storage prefix.

    Args:
        gcs_prefix: A gs:// prefix, e.g. "gs://my-bucket/products/". If no
            bucket scheme is given, the default CANDIDATE_BUCKET is used.
        max_results: Maximum number of images to return.

    Returns a JSON string: {"images": [{"name","gs_uri","url"}, ...]}.
    Each image's gs_uri can be passed to run_fidelity_eval as a reference;
    url is a signed URL for display in an A2UI Image component.
    """
    from google.cloud import storage

    if gcs_prefix.startswith("gs://"):
        bucket_name, prefix = _split_gs(gcs_prefix)
    else:
        bucket_name, prefix = _default_bucket(), gcs_prefix.lstrip("/")
    logger.info("🔎 list_gcs_images | bucket=%s prefix=%r max=%d", bucket_name, prefix, max_results)
    if not bucket_name:
        logger.warning("🔎 list_gcs_images | no bucket in prefix and no default set")
        return json.dumps({"error": "No bucket specified and no default bucket set."})

    try:
        client = storage.Client(project=_project())
        images, scanned, signed = [], 0, 0
        for blob in client.list_blobs(bucket_name, prefix=prefix):
            scanned += 1
            if blob.name.endswith("/") or not blob.name.lower().endswith(_IMAGE_EXTS):
                continue
            url = _signed_url(blob)
            if url:
                signed += 1
            images.append(
                {
                    "name": blob.name.rsplit("/", 1)[-1],
                    "gs_uri": f"gs://{bucket_name}/{blob.name}",
                    "url": url,
                }
            )
            if len(images) >= max_results:
                break
    except Exception as e:
        logger.error("🔎 list_gcs_images | GCS error on bucket=%s: %s", bucket_name, e, exc_info=True)
        return json.dumps({"error": f"Could not list gs://{bucket_name}/{prefix}: {e}"})

    logger.info(
        "🔎 list_gcs_images | scanned=%d images=%d signed_urls=%d%s",
        scanned, len(images), signed,
        " ⚠️ signed URLs failed — images may not display" if images and signed == 0 else "",
    )
    if not images:
        logger.warning("🔎 list_gcs_images | no images found under gs://%s/%s", bucket_name, prefix)
    return json.dumps({"images": images})


async def ingest_uploaded_image_tool(tool_context=None) -> str:
    """Persist the user's uploaded image to GCS and return its gs:// URI.

    Call this when the user has uploaded/dragged an image into the chat and
    wants it evaluated. The returned gs_uri is then passed to run_fidelity_eval
    as a reference. Returns JSON: {"gs_uri": "..."} or {"error": "..."}.
    """
    logger.info("📤 ingest_uploaded_image | start")
    if not tool_context:
        logger.warning("📤 ingest_uploaded_image | no tool_context")
        return json.dumps({"error": "No tool context available."})
    from google.cloud import storage

    part, filename, source = None, None, None

    # 1. Formal artifacts (staged uploads)
    artifact_keys = await tool_context.list_artifacts()
    logger.info("📤 ingest_uploaded_image | artifacts=%s", list(artifact_keys or []))
    if artifact_keys:
        filename = artifact_keys[-1]
        part = await tool_context.load_artifact(filename)
        source = "artifact"

    # 2. Fallback: scan session history for an inline/file image part
    if not part:
        for event in reversed(tool_context.session.events):
            if event.author == "user" and event.content and event.content.parts:
                for p in event.content.parts:
                    if p.inline_data or p.file_data:
                        part, filename, source = p, "uploaded_image", "session-scan"
                        break
            if part:
                break

    if not part:
        logger.warning("📤 ingest_uploaded_image | no image part found (artifacts or session)")
        return json.dumps({"error": "No uploaded image found. Please attach one."})
    logger.info("📤 ingest_uploaded_image | source=%s filename=%s", source, filename)

    data_bytes, mime_type = None, "image/png"
    if part.inline_data:
        mime_type = part.inline_data.mime_type or mime_type
        data_bytes = part.inline_data.data
    elif part.file_data and part.file_data.file_uri:
        # Already in GCS — just hand back the URI.
        logger.info("📤 ingest_uploaded_image | already in GCS -> %s", part.file_data.file_uri)
        return json.dumps({"gs_uri": part.file_data.file_uri})

    if not data_bytes:
        logger.warning("📤 ingest_uploaded_image | could not read bytes for %s", filename)
        return json.dumps({"error": f"Could not read bytes for {filename}."})

    bucket_name = _default_bucket()
    if not bucket_name:
        logger.warning("📤 ingest_uploaded_image | no CANDIDATE_BUCKET/BUCKET_NAME set")
        return json.dumps({"error": "No CANDIDATE_BUCKET/BUCKET_NAME set for uploads."})
    ext = (mime_type.split("/")[-1] or "png").replace("jpeg", "jpg")
    blob_name = f"uploads/{uuid.uuid4().hex[:8]}_{filename or 'image'}.{ext}"
    logger.info(
        "📤 ingest_uploaded_image | uploading %d bytes (%s) -> gs://%s/%s",
        len(data_bytes), mime_type, bucket_name, blob_name,
    )
    try:
        blob = storage.Client(project=_project()).bucket(bucket_name).blob(blob_name)
        blob.upload_from_string(data_bytes, content_type=mime_type)
    except Exception as e:
        logger.error("📤 ingest_uploaded_image | upload failed to bucket=%s: %s", bucket_name, e, exc_info=True)
        return json.dumps({"error": f"Upload to gs://{bucket_name} failed: {e}"})
    gs_uri = f"gs://{bucket_name}/{blob_name}"
    logger.info("📤 ingest_uploaded_image | ✔ uploaded -> %s", gs_uri)
    return json.dumps({"gs_uri": gs_uri, "url": _signed_url(blob)})


def get_eval_defaults() -> str:
    """Return the current server-side evaluation defaults for pre-filling the
    settings UI. Call this before rendering the "Evaluation settings" panel so
    the sliders/fields show the true configured values.

    Returns a JSON string: {"threshold", "max_retries", "media_type",
    "description_model", "image_model"}.
    """
    try:
        from evaluation_wrapper import EvalConfig
    except ImportError:
        from .evaluation_wrapper import EvalConfig  # type: ignore
    cfg = EvalConfig.from_settings()
    return json.dumps(
        {
            "threshold": cfg.threshold,
            "max_retries": cfg.max_retries,
            "media_type": cfg.media_type,
            "description_model": cfg.description_model,
            "image_model": os.environ.get("IMAGE_MODEL", "gemini-3.1-flash-image"),
        }
    )


_IMAGE_MODELS = (
    "gemini-3.1-flash-lite-image",
    "gemini-3.1-flash-image",
    "gemini-3-pro-image",
)


def run_fidelity_eval(
    reference_uris,
    sku_id: str = "",
    user_prompt: str = "",
    threshold: float = 0.0,
    max_retries: int = 0,
    image_model: str = "",
) -> str:
    """Run the full product-fidelity evaluation loop on reference image(s).

    Loop: describe reference -> generate candidate (gemini-3.1-flash-image)
    -> Gecko score vs description -> threshold -> refine -> retry.

    Args:
        reference_uris: One or more gs:// URIs of the product reference image(s).
            Accepts a list or a comma/space-separated string.
        sku_id: Optional product identifier (auto-generated if empty).
        user_prompt: Optional creative direction for generation.
        threshold: Optional passing-score override in (0, 1]. 0 = use the
            server default (from the settings widget or .env).
        max_retries: Optional max-attempts override (>= 1). 0 = use the default.
        image_model: Optional image model id for candidate generation. One of
            gemini-3.1-flash-lite-image (fastest/cheapest),
            gemini-3.1-flash-image (balanced default),
            gemini-3-pro-image (highest quality). Invalid/blank = env default.

    Returns a JSON string with keys: sku_id, passed, final_score, attempts[]
    (each with score, candidate_uri, candidate_url, passing/failing verdicts),
    ground_truth_description, reference_display[], settings_used.
    """
    logger.info("🧪 run_fidelity_eval | raw refs=%r sku=%r thr=%s retries=%s", reference_uris, sku_id, threshold, max_retries)
    if isinstance(reference_uris, str):
        reference_uris = [u for u in re.split(r"[,\s]+", reference_uris) if u]
    reference_uris = [u for u in (reference_uris or []) if str(u).startswith("gs://")]
    if not reference_uris:
        logger.warning("🧪 run_fidelity_eval | no valid gs:// reference URIs")
        return json.dumps({"error": "Provide at least one gs:// reference URI."})
    if not sku_id:
        sku_id = "sku-" + uuid.uuid4().hex[:6]

    try:
        from evaluation_wrapper import EvalConfig, EvalPipeline
    except ImportError:
        from .evaluation_wrapper import EvalConfig, EvalPipeline  # type: ignore
    try:
        from generate import generate_candidate_image
    except ImportError:
        from .generate import generate_candidate_image  # type: ignore

    config = EvalConfig.from_settings()
    # Apply per-run overrides from the settings widget (0 = keep default).
    try:
        if threshold and 0 < float(threshold) <= 1:
            config.threshold = float(threshold)
        if max_retries and int(max_retries) >= 1:
            config.max_retries = int(max_retries)
    except (TypeError, ValueError):
        pass
    # Validate the image model against the known set; blank/unknown -> env default.
    env_image_model = os.environ.get("IMAGE_MODEL", "gemini-3.1-flash-image")
    chosen_image_model = image_model if image_model in _IMAGE_MODELS else env_image_model
    if image_model and image_model not in _IMAGE_MODELS:
        logger.warning("🧪 run_fidelity_eval | unknown image_model %r → using %s", image_model, env_image_model)
    if not config.bucket_name:
        logger.warning("🧪 run_fidelity_eval | no CANDIDATE_BUCKET/BUCKET_NAME set")
        return json.dumps({"error": "CANDIDATE_BUCKET/BUCKET_NAME must be set."})

    logger.info(
        "🧪 run_fidelity_eval | sku=%s refs=%d | threshold=%.2f max_retries=%d | "
        "creative_direction=%r | desc_model=%s@%s image_model=%s@global "
        "gecko_region=%s bucket=%s",
        sku_id, len(reference_uris), config.threshold, config.max_retries,
        user_prompt or "(none)",
        config.description_model, config.description_location,
        chosen_image_model, config.location, config.bucket_name,
    )
    pipeline = EvalPipeline(generate_fn=generate_candidate_image, config=config)
    try:
        result = pipeline.run(
            reference_uris=reference_uris, sku_id=sku_id, user_prompt=user_prompt,
            image_model=chosen_image_model,
        )
    except Exception as e:
        logger.error("🧪 run_fidelity_eval | pipeline failed: %s", e, exc_info=True)
        return json.dumps({"error": f"Evaluation failed: {e}"})

    # Attach signed URLs so the LLM can render images in A2UI.
    result["reference_display"] = [
        {"gs_uri": u, "url": _signed_url_for_uri(u)} for u in reference_uris
    ]
    ref_signed = sum(1 for r in result["reference_display"] if r["url"])
    cand_signed = 0
    for attempt in result.get("attempts", []):
        attempt["candidate_url"] = _signed_url_for_uri(attempt.get("candidate_uri", ""))
        if attempt["candidate_url"]:
            cand_signed += 1

    result["settings_used"] = {
        "threshold": config.threshold,
        "max_retries": config.max_retries,
        "image_model": chosen_image_model,
    }
    attempts = result.get("attempts", [])
    logger.info(
        "🧪 run_fidelity_eval | ✔ sku=%s passed=%s final_score=%s attempts=%d | "
        "signed refs=%d/%d candidates=%d/%d",
        sku_id, result.get("passed"), result.get("final_score"), len(attempts),
        ref_signed, len(result["reference_display"]), cand_signed, len(attempts),
    )
    for a in attempts:
        if a.get("error"):
            logger.warning("🧪   attempt %s: ERROR %s", a.get("attempt"), a.get("error"))
        else:
            logger.info(
                "🧪   attempt %s: score=%s pass=%d fail=%d",
                a.get("attempt"), a.get("score"),
                len(a.get("passing_verdicts", [])), len(a.get("failing_verdicts", [])),
            )
    return json.dumps(result)
