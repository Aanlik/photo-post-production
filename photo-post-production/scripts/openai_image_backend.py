"""Legacy OpenAI API image-editing adapter.

The current project mode is ChatGPT's built-in conversation image tool. This
module is retained as an inactive, reversible implementation reference only;
``capability_registry`` will not select it in normal Hybrid runs.

The module deliberately uses only Python's standard library.  It does not
upload pixels during capability discovery unless ``network=True`` is passed to
``probe_openai_image_backend``.  Actual image editing requires both an API key
and an explicit cloud-generation approval, so a configured key alone never
authorizes an upload.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import secrets
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ENDPOINT = "https://api.openai.com/v1/images/edits"
DEFAULT_MODEL = "gpt-image-2"
RAW_SUFFIXES = {".arw", ".cr2", ".cr3", ".dng", ".nef", ".nrw", ".orf", ".raf", ".rw2", ".pef", ".srw"}
SUPPORTED_INPUT_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class CloudGenerationDisabled(PermissionError):
    """Raised when a caller has not explicitly allowed cloud pixel transfer."""


class BackendRequestError(RuntimeError):
    """Raised when the OpenAI image endpoint returns an unusable response."""


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_public_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or parsed.netloc.casefold() not in {"api.openai.com", "api.openai.com:443"}:
        raise BackendRequestError("refusing_non_openai_output_url")
    return value


@dataclass(frozen=True)
class OpenAIImageConfig:
    """Runtime configuration with secrets excluded from serializable output."""

    api_key: str | None = None
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    timeout_seconds: int = 120
    allow_cloud_generation: bool = False
    quality: str = "high"
    size: str = "auto"
    output_format: str = "png"
    input_fidelity: str | None = None

    @classmethod
    def from_env(cls, allow_cloud_generation: bool | None = None) -> "OpenAIImageConfig":
        configured_allowance = _env_bool("PHOTO_ALLOW_CLOUD_GENERATION", False)
        configured_format = os.environ.get("PHOTO_OPENAI_OUTPUT_FORMAT", "png").strip().casefold() or "png"
        if configured_format == "jpg":
            configured_format = "jpeg"
        return cls(
            api_key=os.environ.get("OPENAI_API_KEY") or None,
            endpoint=os.environ.get("OPENAI_IMAGE_EDIT_ENDPOINT", DEFAULT_ENDPOINT).strip() or DEFAULT_ENDPOINT,
            model=os.environ.get("OPENAI_IMAGE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            timeout_seconds=_env_int("PHOTO_OPENAI_TIMEOUT_SECONDS", 120, minimum=10),
            allow_cloud_generation=configured_allowance if allow_cloud_generation is None else bool(allow_cloud_generation),
            quality=os.environ.get("PHOTO_OPENAI_IMAGE_QUALITY", "high").strip() or "high",
            size=os.environ.get("PHOTO_OPENAI_IMAGE_SIZE", "auto").strip() or "auto",
            output_format=configured_format,
            input_fidelity=os.environ.get("PHOTO_OPENAI_INPUT_FIDELITY") or None,
        )

    def public_record(self) -> dict[str, Any]:
        return {
            "backend": "openai-image",
            "model": self.model,
            "endpoint_host": urllib.parse.urlparse(self.endpoint).netloc,
            "api_key_configured": bool(self.api_key),
            "allow_cloud_generation": self.allow_cloud_generation,
            "quality": self.quality,
            "size": self.size,
            "output_format": self.output_format,
            "input_fidelity": self.input_fidelity,
            "locality": "cloud",
        }


def _model_endpoint(config: OpenAIImageConfig) -> str:
    parsed = urllib.parse.urlparse(config.endpoint)
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/")
    if path.endswith("/images/edits"):
        path = path[: -len("/images/edits")] + "/models/" + urllib.parse.quote(config.model, safe="")
    else:
        path = "/v1/models/" + urllib.parse.quote(config.model, safe="")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def probe_openai_image_backend(
    timeout: int = 10,
    config: OpenAIImageConfig | None = None,
    network: bool = True,
) -> dict[str, Any]:
    """Return a redacted health record for the configured cloud backend."""

    config = config or OpenAIImageConfig.from_env()
    record: dict[str, Any] = {
        **config.public_record(),
        "available": False,
        "healthy": False,
        "verified": False,
        "reason": None,
    }
    if not config.api_key:
        record["reason"] = "openai_api_key_not_configured"
        return record
    if not config.allow_cloud_generation:
        record["reason"] = "cloud_generation_not_explicitly_enabled"
        return record
    if not network:
        record.update({"available": True, "healthy": None, "verified": False, "reason": "configuration_only_probe"})
        return record
    url = _model_endpoint(config)
    if not url:
        record["reason"] = "invalid_openai_endpoint"
        return record
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {config.api_key}"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=max(1, timeout)) as response:
            response.read(1)
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as error:
        record["reason"] = f"openai_health_http_{error.code}"
        return record
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        record["reason"] = f"openai_health_network_{type(error).__name__}"
        return record
    if status < 200 or status >= 300:
        record["reason"] = f"openai_health_status_{status}"
        return record
    record.update({"available": True, "healthy": True, "verified": True, "reason": None})
    return record


def _multipart_form(fields: dict[str, str], files: list[tuple[str, Path]]) -> tuple[bytes, str]:
    boundary = "----PhotoSkill" + secrets.token_hex(16)
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    for field_name, path in files:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field_name}"; filename="{path.name}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            path.read_bytes(),
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _output_path(output_dir: Path, source: Path, output_format: str) -> Path:
    suffix = ".jpg" if output_format in {"jpg", "jpeg"} else f".{output_format}"
    stem = f"{source.stem}.openai-edit"
    candidate = output_dir / f"{stem}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = output_dir / f"{stem}-{counter:02d}{suffix}"
        counter += 1
    return candidate


def _decode_result(payload: dict[str, Any], config: OpenAIImageConfig) -> tuple[bytes, str | None]:
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise BackendRequestError("openai_response_missing_data")
    item = data[0]
    encoded = item.get("b64_json")
    if isinstance(encoded, str) and encoded:
        try:
            return base64.b64decode(encoded, validate=True), None
        except (ValueError, base64.binascii.Error) as error:
            raise BackendRequestError("openai_response_invalid_base64") from error
    url = item.get("url")
    if isinstance(url, str) and url:
        safe_url = _safe_public_url(url)
        request = urllib.request.Request(safe_url, headers={"User-Agent": "photo-post-production/1"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
                return response.read(), safe_url
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise BackendRequestError("openai_output_download_failed") from error
    raise BackendRequestError("openai_response_missing_image_payload")


def edit_image(
    input_path: str,
    prompt: str,
    output_dir: str,
    mask_path: str | None = None,
    operation_id: str | None = None,
    allow_cloud_generation: bool = False,
    config: OpenAIImageConfig | None = None,
    extra_fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Edit one derived image and write a result plus a provenance sidecar.

    RAW files are intentionally rejected here.  The caller must provide a
    Lightroom/Camera Raw-derived PNG/JPEG/WebP so the original RAW remains the
    immutable source and color-management decisions stay in the local stage.
    """

    config = config or OpenAIImageConfig.from_env(allow_cloud_generation=allow_cloud_generation)
    if not allow_cloud_generation or not config.allow_cloud_generation:
        raise CloudGenerationDisabled("cloud_generation_requires_explicit_allowance")
    if not config.api_key:
        raise BackendRequestError("openai_api_key_not_configured")
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.casefold() in RAW_SUFFIXES:
        raise ValueError("raw_input_requires_local_derived_preview")
    if source.suffix.casefold() not in SUPPORTED_INPUT_SUFFIXES:
        raise ValueError(f"unsupported_generation_input:{source.suffix.casefold()}")
    if config.output_format not in {"png", "jpeg", "jpg", "webp"}:
        raise ValueError(f"unsupported_output_format:{config.output_format}")
    mask = Path(mask_path).expanduser().resolve() if mask_path else None
    if mask is not None and not mask.is_file():
        raise FileNotFoundError(mask)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be non-empty")
    destination_dir = Path(output_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    output = _output_path(destination_dir, source, config.output_format)
    fields = {
        "model": config.model,
        "prompt": prompt.strip(),
        "quality": config.quality,
        "size": config.size,
        "output_format": config.output_format,
    }
    if config.input_fidelity:
        fields["input_fidelity"] = config.input_fidelity
    if extra_fields:
        fields.update({str(key): str(value) for key, value in extra_fields.items()})
    files = [("image", source)]
    if mask is not None:
        files.append(("mask", mask))
    body, content_type = _multipart_form(fields, files)
    request = urllib.request.Request(
        config.endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": content_type,
            "Accept": "application/json",
            "User-Agent": "photo-post-production/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            response_bytes = response.read()
            request_id = response.headers.get("x-request-id")
    except urllib.error.HTTPError as error:
        detail = error.read(512).decode("utf-8", "replace")
        raise BackendRequestError(f"openai_edit_http_{error.code}:{detail[:160]}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise BackendRequestError(f"openai_edit_network_{type(error).__name__}") from error
    try:
        payload = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackendRequestError("openai_response_invalid_json") from error
    image_bytes, output_url = _decode_result(payload, config)
    with tempfile.NamedTemporaryFile(prefix="photo-skill-", suffix=output.suffix, dir=destination_dir, delete=False) as temporary:
        temporary.write(image_bytes)
        temporary_path = Path(temporary.name)
    temporary_path.replace(output)
    op_id = operation_id or f"openai-edit-{source.stem}"
    record = {
        "operation_id": op_id,
        "status": "completed",
        "backend": "openai-image",
        "backend_locality": "cloud",
        "model": config.model,
        "model_version": config.model,
        "software": "OpenAI Images API",
        "prompt": prompt.strip(),
        "input_path": str(source),
        "input_sha256": _sha256(source),
        "mask_path": str(mask) if mask else None,
        "mask_sha256": _sha256(mask) if mask else None,
        "output_path": str(output),
        "output_sha256": _sha256(output),
        "output_format": config.output_format,
        "output_url_used": bool(output_url),
        "request_id": request_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "disclosure": "图片像素已发送至 OpenAI 图像 API；原始 RAW 未上传。",
    }
    sidecar = output.with_suffix(output.suffix + ".provenance.json")
    sidecar.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"output_path": str(output), "provenance_path": str(sidecar), "record": record}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="OpenAI 图像编辑后端健康检查或单图编辑")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--mask")
    parser.add_argument("--prompt")
    parser.add_argument("--output", help="输出目录")
    parser.add_argument("--operation-id")
    parser.add_argument("--allow-cloud-generation", action="store_true")
    args = parser.parse_args()
    config = OpenAIImageConfig.from_env(allow_cloud_generation=args.allow_cloud_generation)
    if args.probe or not args.input:
        print(json.dumps(probe_openai_image_backend(config=config, network=not args.no_network), ensure_ascii=False, indent=2))
        return 0
    if not args.prompt or not args.output:
        parser.error("--input 模式需要同时提供 --prompt 和 --output")
    result = edit_image(
        args.input,
        args.prompt,
        args.output,
        mask_path=args.mask,
        operation_id=args.operation_id,
        allow_cloud_generation=args.allow_cloud_generation,
        config=config,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
