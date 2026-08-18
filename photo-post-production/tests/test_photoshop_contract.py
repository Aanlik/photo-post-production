"""Contract tests for the Photoshop adapter health and operation boundary."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import check_photoshop_adapter as adapter  # noqa: E402


def healthy_payload() -> dict:
    return {
        "available": True,
        "bridge_version": "dcc-mcp-photoshop/0.2.0",
        "supports_masks": True,
        "supports_mask_validation": True,
        "supports_layers": True,
        "supports_non_destructive_layers": True,
        "supports_history": True,
        "supports_export": True,
        "supports_generative_control": True,
        "supports_generative_reporting": True,
        "supports_operation_id": True,
        "supports_operation_manifest": True,
        "backend_locality": "local",
        "generative_backend_healthy": True,
        "generative_backend_locality": "local",
    }


def healthy_broker_payload() -> dict:
    return {
        "bridge": {
            "connected": True,
            "host": "photoshop",
            "plugin_id": "com.adobepy.bridge.photoshop",
            "uxp_loaded": True,
            "version": "dcc-mcp-photoshop/0.2.0",
        },
        "capabilities": {
            key: value
            for key, value in healthy_payload().items()
            if key.startswith("supports_") or key == "backend_locality"
        },
        "generative_backend": {
            "healthy": True,
            "locality": "local",
            "name": "verified-local-generative-backend",
            "version": "1.0",
        },
    }


def independent_developer_mode_probe(source_type: str = "photoshop-system-settings-readback") -> dict:
    return {
        "enabled": True,
        "source_type": source_type,
        "probe_marker": "independent-read-only-v1",
        "setting_key": "UXPDeveloperMode",
    }


def healthy_cli_payload() -> dict:
    return {
        "sessions": [{
            "dcc_type": "photoshop",
            "connected": True,
            "tools": [
                {"name": "photoshop_list_layers"},
                {"name": "photoshop_export_document"},
            ],
        }],
    }


def valid_operation_payload(root: Path, operation_id: str = "ps-central-building-v1") -> dict:
    before = root / f"{operation_id}-before.tif"
    after = root / f"{operation_id}-after.tif"
    mask = root / f"{operation_id}-mask.png"
    export = root / f"{operation_id}-candidate.jpg"
    before.write_bytes(b"before")
    after.write_bytes(b"after")
    mask.write_bytes(b"mask")
    export.write_bytes(b"jpeg")
    entry = {
        "operation_id": operation_id,
        "depends_on": [],
        "type": "relight-subject",
        "region": "central-building",
        "mask_reference": str(mask),
        "input_layer_id": "layer-input",
        "output_layer_id": "layer-central-building",
        "mask_sha256": hashlib.sha256(mask.read_bytes()).hexdigest(),
        "parameters": {"exposure": 0.15},
        "risk": "low",
        "before_path": str(before),
        "before_sha256": hashlib.sha256(before.read_bytes()).hexdigest(),
        "after_path": str(after),
        "after_sha256": hashlib.sha256(after.read_bytes()).hexdigest(),
        "generative": False,
    }
    payload = {
        "requested_operation_id": operation_id,
        "operation_id": operation_id,
        "status": "completed",
        "idempotent_replay": False,
        "execution": {
            "backend": "dcc-mcp-photoshop",
            "software": "Adobe Photoshop",
            "software_version": "27.9.1",
            "bridge_version": "dcc-mcp-photoshop/0.2.0",
            "locality": "local",
        },
        "document_evidence": {
            "input_document_id": "doc-1",
            "output_document_id": "doc-1",
            "input_layer_id": "layer-input",
            "output_layer_id": "layer-central-building",
            "editable": True,
            "layered": True,
            "non_destructive": True,
            "flattened": False,
        },
        "export_evidence": {
            "status": "completed",
            "path": str(export),
            "sha256": hashlib.sha256(export.read_bytes()).hexdigest(),
            "format": "JPEG",
            "source_master_sha256": entry["after_sha256"],
        },
        "mask_validation": {
            "status": "valid",
            "dimensions_match": True,
            "edges_checked": True,
            "artifact_warnings": [],
        },
        "generative": {
            "used": False,
            "reported": True,
            "controlled": True,
            "backend_healthy": True,
            "backend_locality": "local",
            "model": None,
            "model_version": None,
            "prompt": None,
        },
        "operation_manifest": {
            "status": "valid",
            "operation_id": operation_id,
            "manifest_hash": hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest(),
            "entry": entry,
        },
        "warnings": [],
    }
    return payload


class PhotoshopHealthContractTests(unittest.TestCase):
    def test_healthy_adapter_enables_fine_edit_mode(self) -> None:
        with mock.patch.object(adapter, "_discover_adapter_health", return_value=healthy_payload()):
            result = adapter.check_health()

        required = {
            "available",
            "bridge_version",
            "supports_masks",
            "supports_layers",
            "supports_non_destructive_layers",
            "supports_history",
            "supports_export",
            "supports_generative_control",
            "supports_operation_id",
        }
        self.assertTrue(required.issubset(result))
        self.assertTrue(result["available"])
        self.assertTrue(result["fine_edit_mode"])
        self.assertTrue(result["generative_operations_enabled"])
        self.assertEqual(result["mode"], "fine-edit")
        self.assertIsNone(result["downgrade_reason"])

    def test_history_mode_may_defer_jpeg_until_final_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = valid_operation_payload(root, "history-operation")
            payload["is_final_operation"] = False
            payload["checkpoint_mode"] = "history"
            payload["history_snapshot_name"] = "phase-global"
            payload.pop("export_evidence")
            entry = payload["operation_manifest"]["entry"]
            entry.pop("after_path")
            entry.pop("after_sha256")
            entry["history_snapshot_name"] = "phase-global"
            payload["operation_manifest"]["manifest_hash"] = adapter._canonical_manifest_hash(entry)

            result = adapter.normalize_operation_result(payload)

            self.assertTrue(result["valid"], result["errors"])
            self.assertEqual(result["export_evidence"]["status"], "not-run")

    def test_production_discovery_can_return_healthy_structured_result(self) -> None:
        executable_paths = {
            "rustc": "/toolchain/rustc",
            "cargo": "/toolchain/cargo",
            "adobepy": "/tools/adobepy",
            "dcc-mcp-photoshop": "/tools/dcc-mcp-photoshop",
            "dcc-mcp-cli": "/tools/dcc-mcp-cli",
        }
        with (
            mock.patch.object(adapter, "_photoshop_installation", return_value=("/Applications/Photoshop.app", "27.9.1")),
            mock.patch.object(adapter, "_uxp_developer_tool_path", return_value="/Applications/UXP Developer Tool.app"),
            mock.patch.object(adapter, "_staged_bridge_manifest", return_value="/bridge/manifest.json"),
            mock.patch.object(adapter, "_read_json_url", return_value=(healthy_broker_payload(), None)),
            mock.patch.object(adapter, "_run_read_only_cli", return_value=(healthy_cli_payload(), None)),
            mock.patch.object(adapter, "_probe_developer_mode", return_value=independent_developer_mode_probe()),
            mock.patch.object(adapter, "_process_running", return_value=True),
            mock.patch.object(adapter.shutil, "which", side_effect=executable_paths.get),
        ):
            result = adapter.check_health()

        self.assertTrue(result["available"])
        self.assertEqual(result["bridge_version"], "dcc-mcp-photoshop/0.2.0")
        self.assertTrue(result["fine_edit_mode"])
        self.assertTrue(result["generative_backend_healthy"])
        self.assertTrue(result["generative_operations_enabled"])
        self.assertTrue(result["supports_operation_manifest"])
        self.assertEqual(result["backend_locality"], "local")
        self.assertEqual(result["generative_backend_locality"], "local")
        self.assertTrue(result["discovery"]["uxp_bridge_loaded"])
        self.assertTrue(result["discovery"]["photoshop_developer_mode_enabled"])
        self.assertTrue(result["discovery"]["mcp_tool_discovery_succeeded"])

    def test_substrings_do_not_prove_bridge_or_mcp_discovery(self) -> None:
        broker = {"note": "photoshop com.adobepy.bridge.photoshop developer mode true"}
        cli = {"message": "photoshop tools are somewhere in this text"}
        with (
            mock.patch.object(adapter, "_photoshop_installation", return_value=("/Applications/Photoshop.app", "27.9.1")),
            mock.patch.object(adapter, "_read_json_url", return_value=(broker, None)),
            mock.patch.object(adapter, "_run_read_only_cli", return_value=(cli, None)),
            mock.patch.object(
                adapter,
                "_probe_developer_mode",
                return_value={"enabled": None, "source_type": None, "probe_marker": None},
            ),
            mock.patch.object(adapter.shutil, "which", return_value="/present"),
        ):
            result = adapter.check_health()

        self.assertFalse(result["available"])
        self.assertFalse(result["discovery"]["uxp_bridge_loaded"])
        self.assertIsNone(result["discovery"]["photoshop_developer_mode_enabled"])
        self.assertFalse(result["discovery"]["mcp_photoshop_session_discovered"])
        self.assertFalse(result["discovery"]["mcp_tool_discovery_succeeded"])

    def test_bridge_self_report_does_not_independently_prove_developer_mode(self) -> None:
        broker = healthy_broker_payload()
        broker["developer_mode"] = {"enabled": True, "source": "bridge-self-report"}
        with (
            mock.patch.object(adapter, "_photoshop_installation", return_value=("/Applications/Photoshop.app", "27.9.1")),
            mock.patch.object(adapter, "_uxp_developer_tool_path", return_value="/Applications/UXP Developer Tool.app"),
            mock.patch.object(adapter, "_staged_bridge_manifest", return_value="/bridge/manifest.json"),
            mock.patch.object(adapter, "_read_json_url", return_value=(broker, None)),
            mock.patch.object(adapter, "_run_read_only_cli", return_value=(healthy_cli_payload(), None)),
            mock.patch.object(adapter, "_probe_developer_mode", return_value={
                "enabled": None,
                "source_type": None,
                "probe_marker": "independent-read-only-v1",
            }),
            mock.patch.object(adapter.shutil, "which", return_value="/present"),
        ):
            result = adapter.check_health()

        self.assertFalse(result["available"])
        self.assertIsNone(result["discovery"]["photoshop_developer_mode_enabled"])
        self.assertIsNone(result["discovery"]["photoshop_developer_mode_evidence_source"])
        self.assertIn("verified Photoshop Developer Mode", result["downgrade_reason"]["details"])

    def test_unallowlisted_developer_mode_probe_sources_are_rejected(self) -> None:
        for source_type in ("bridge-self-report", "arbitrary-string"):
            broker = healthy_broker_payload()
            broker["developer_mode"] = {"enabled": True, "source": source_type}
            with self.subTest(source_type=source_type):
                with (
                    mock.patch.object(adapter, "_photoshop_installation", return_value=("/Applications/Photoshop.app", "27.9.1")),
                    mock.patch.object(adapter, "_uxp_developer_tool_path", return_value="/Applications/UXP Developer Tool.app"),
                    mock.patch.object(adapter, "_staged_bridge_manifest", return_value="/bridge/manifest.json"),
                    mock.patch.object(adapter, "_read_json_url", return_value=(broker, None)),
                    mock.patch.object(adapter, "_run_read_only_cli", return_value=(healthy_cli_payload(), None)),
                    mock.patch.object(adapter, "_probe_developer_mode", return_value=independent_developer_mode_probe(source_type)),
                    mock.patch.object(adapter.shutil, "which", return_value="/present"),
                ):
                    result = adapter.check_health()

                self.assertFalse(result["available"])
                self.assertIsNone(result["discovery"]["photoshop_developer_mode_enabled"])
                self.assertIsNone(result["discovery"]["photoshop_developer_mode_evidence_source"])

    def test_absent_photoshop_has_accurate_downgrade(self) -> None:
        with (
            mock.patch.object(adapter, "_photoshop_installation", return_value=(None, None)),
            mock.patch.object(adapter, "_read_json_url", return_value=(None, "connection refused")),
            mock.patch.object(adapter.shutil, "which", return_value=None),
        ):
            result = adapter.check_health()

        self.assertFalse(result["available"])
        self.assertEqual(result["downgrade_reason"]["code"], "photoshop_not_installed")
        self.assertEqual(result["downgrade_reason"]["prerequisite"], "Adobe Photoshop application installation")
        self.assertIn("not installed", result["downgrade_reason"]["message"])

    def test_unavailable_adapter_returns_structured_downgrade(self) -> None:
        payload = {
            "available": False,
            "bridge_version": None,
            "downgrade_reason": {
                "code": "broker_unreachable",
                "prerequisite": "adobepy broker",
                "message": "No broker responded at http://127.0.0.1:47391.",
            },
            "discovery": {"photoshop_installed": True, "broker_reachable": False},
        }
        with mock.patch.object(adapter, "_discover_adapter_health", return_value=payload):
            result = adapter.check_health()

        self.assertFalse(result["available"])
        self.assertFalse(result["fine_edit_mode"])
        self.assertEqual(result["mode"], "global-only")
        self.assertEqual(result["downgrade_reason"]["code"], "broker_unreachable")
        self.assertEqual(result["downgrade_reason"]["prerequisite"], "adobepy broker")
        self.assertFalse(result["smoke_test"]["eligible"])

    def test_discovery_exception_is_classified(self) -> None:
        with mock.patch.object(adapter, "_discover_adapter_health", side_effect=OSError("probe failed")):
            result = adapter.check_health()

        self.assertFalse(result["available"])
        self.assertEqual(result["mode"], "global-only")
        self.assertEqual(result["downgrade_reason"]["code"], "health_check_error")
        self.assertEqual(
            result["downgrade_reason"]["prerequisite"],
            "Photoshop adapter health checker",
        )
        self.assertIn("OSError: probe failed", result["downgrade_reason"]["details"])

    def test_malformed_health_payload_is_classified(self) -> None:
        payload = healthy_payload()
        payload.pop("supports_masks")
        payload["supports_export"] = "yes"
        with mock.patch.object(adapter, "_discover_adapter_health", return_value=payload):
            result = adapter.check_health()

        self.assertFalse(result["available"])
        self.assertEqual(result["mode"], "global-only")
        self.assertEqual(result["downgrade_reason"]["code"], "malformed_health_payload")
        self.assertIn("missing:supports_masks", result["downgrade_reason"]["details"])
        self.assertIn("invalid_boolean:supports_export", result["downgrade_reason"]["details"])

    def test_bridge_without_generative_reporting_or_control_is_downgraded(self) -> None:
        payload = healthy_payload()
        payload["supports_generative_control"] = False
        payload["supports_generative_reporting"] = False
        with mock.patch.object(adapter, "_discover_adapter_health", return_value=payload):
            result = adapter.check_health()

        self.assertFalse(result["available"])
        self.assertFalse(result["fine_edit_mode"])
        self.assertFalse(result["generative_operations_enabled"])
        self.assertEqual(result["mode"], "global-only")
        self.assertEqual(result["downgrade_reason"]["code"], "missing_required_capability")
        self.assertEqual(
            result["downgrade_reason"]["details"],
            ["supports_generative_control", "supports_generative_reporting"],
        )

    def test_unhealthy_generative_backend_does_not_enable_generation(self) -> None:
        payload = healthy_payload()
        payload["generative_backend_healthy"] = False
        with mock.patch.object(adapter, "_discover_adapter_health", return_value=payload):
            result = adapter.check_health()

        self.assertTrue(result["available"])
        self.assertTrue(result["fine_edit_mode"])
        self.assertFalse(result["generative_operations_enabled"])
        self.assertEqual(result["generative_downgrade_reason"]["code"], "generative_backend_unhealthy")


class PhotoshopOperationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_valid_masked_operation_is_normalized(self) -> None:
        payload = valid_operation_payload(self.root)

        result = adapter.normalize_operation_result(payload)

        self.assertTrue(result["valid"])
        self.assertEqual(result["operation_id"], "ps-central-building-v1")
        self.assertEqual(result["requested_operation_id"], "ps-central-building-v1")
        self.assertEqual(result["operation_manifest"]["status"], "valid")
        self.assertTrue(result["document_evidence"]["non_destructive"])
        self.assertEqual(result["export_evidence"]["format"], "JPEG")
        self.assertEqual(result["errors"], [])

    def test_valid_generative_operation_has_explicit_provenance(self) -> None:
        payload = valid_operation_payload(self.root, "ps-generative-sign-repair-v1")
        provenance = {
            "model": "verified-model",
            "model_version": "1.0",
            "prompt": "Repair only the selected sign edge.",
        }
        payload["generative"].update({"used": True, **provenance})
        payload["operation_manifest"]["entry"].update({
            "generative": True,
            **provenance,
        })
        payload["operation_manifest"]["manifest_hash"] = hashlib.sha256(
            json.dumps(payload["operation_manifest"]["entry"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        result = adapter.normalize_operation_result(payload)

        self.assertTrue(result["valid"])
        self.assertTrue(result["generative"]["used"])
        self.assertTrue(result["generative"]["backend_healthy"])
        self.assertEqual(result["execution"]["backend"], "dcc-mcp-photoshop")
        self.assertEqual(result["operation_manifest"]["entry"]["prompt"], provenance["prompt"])
        self.assertEqual(result["errors"], [])

    def test_missing_requested_operation_id_is_rejected(self) -> None:
        payload = valid_operation_payload(self.root)
        payload.pop("requested_operation_id")

        result = adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertIn("missing_requested_operation_id", result["errors"])

    def test_returned_operation_id_must_match_requested_id(self) -> None:
        payload = valid_operation_payload(self.root)
        payload["operation_id"] = "adapter-changed-id"

        result = adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertIn("operation_id_mismatch", result["errors"])

    def test_missing_operation_manifest_is_rejected(self) -> None:
        payload = valid_operation_payload(self.root)
        payload.pop("operation_manifest")

        result = adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertIn("missing_operation_manifest", result["errors"])

    def test_malformed_operation_manifest_is_rejected(self) -> None:
        payload = valid_operation_payload(self.root)
        payload["operation_manifest"] = {"status": "valid", "entry": "not-an-object"}

        result = adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertIn("invalid_manifest_operation_id", result["errors"])
        self.assertIn("invalid_manifest_hash", result["errors"])
        self.assertIn("malformed_manifest_entry", result["errors"])

    def test_manifest_operation_ids_must_match(self) -> None:
        payload = valid_operation_payload(self.root)
        payload["operation_manifest"]["operation_id"] = "foreign-manifest-id"
        payload["operation_manifest"]["entry"]["operation_id"] = "foreign-entry-id"

        result = adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertIn("manifest_operation_id_mismatch", result["errors"])
        self.assertIn("manifest_entry_operation_id_mismatch", result["errors"])

    def test_manifest_entry_requires_complete_operation_evidence(self) -> None:
        payload = valid_operation_payload(self.root)
        payload["operation_manifest"]["entry"].pop("depends_on")
        payload["operation_manifest"]["entry"].pop("before_sha256")

        result = adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertIn("invalid_manifest_entry_depends_on", result["errors"])
        self.assertIn("invalid_manifest_entry_before_sha256", result["errors"])

    def test_non_destructive_document_evidence_is_required(self) -> None:
        payload = valid_operation_payload(self.root)
        payload["document_evidence"]["non_destructive"] = False
        payload["document_evidence"]["flattened"] = True

        result = adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertIn("document_not_non_destructive", result["errors"])
        self.assertIn("document_flattened", result["errors"])

    def test_export_evidence_is_required(self) -> None:
        payload = valid_operation_payload(self.root)
        payload["is_final_operation"] = True
        payload["checkpoint_mode"] = "final"
        payload.pop("export_evidence")

        result = adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertIn("missing_export_evidence", result["errors"])

    def test_missing_mask_validation_is_rejected_structurally(self) -> None:
        payload = {
            "operation_id": "ps-sign-highlights-v1",
            "status": "completed",
            "idempotent_replay": False,
            "generative": {"used": False, "reported": True, "controlled": True},
            "warnings": [],
        }

        result = adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertIn("missing_mask_validation", result["errors"])
        self.assertEqual(result["classification"], "invalid_operation_result")

    def test_malformed_operation_payload_does_not_raise(self) -> None:
        result = adapter.normalize_operation_result({"operation_id": "", "status": 200})

        self.assertFalse(result["valid"])
        self.assertIn("invalid_operation_id", result["errors"])
        self.assertIn("invalid_status", result["errors"])
        self.assertIn("missing_mask_validation", result["errors"])

    def test_unreported_or_uncontrolled_generative_operation_is_rejected(self) -> None:
        payload = valid_operation_payload(self.root, "ps-generative-sign-repair-v1")
        payload["generative"].update({
            "used": True,
            "reported": False,
            "controlled": False,
            "model": None,
            "model_version": None,
            "prompt": None,
        })

        result = adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertIn("generative_operation_not_reported", result["errors"])
        self.assertIn("generative_operation_not_controlled", result["errors"])
        self.assertIn("missing_generative_model", result["errors"])
        self.assertIn("missing_generative_model_version", result["errors"])
        self.assertIn("missing_generative_prompt", result["errors"])

    def test_generative_operation_requires_healthy_local_backend_and_execution_provenance(self) -> None:
        payload = valid_operation_payload(self.root, "ps-generative-sign-repair-v1")
        payload["generative"].update({
            "used": True,
            "model": "model-name",
            "model_version": "model-version",
            "prompt": "Repair only the selected sign edge.",
            "backend_healthy": False,
            "backend_locality": "remote",
        })
        payload["execution"]["backend"] = ""
        payload["execution"]["software_version"] = None
        payload["execution"]["bridge_version"] = ""
        payload["execution"]["locality"] = "remote"

        result = adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertIn("invalid_execution_backend", result["errors"])
        self.assertIn("invalid_execution_software_version", result["errors"])
        self.assertIn("invalid_execution_bridge_version", result["errors"])
        self.assertIn("execution_locality_not_local", result["errors"])
        self.assertIn("generative_backend_unhealthy", result["errors"])
        self.assertIn("generative_backend_locality_not_local", result["errors"])

    def test_adapter_cannot_authorize_remote_generative_locality(self) -> None:
        payload = valid_operation_payload(self.root, "ps-remote-generative-v1")
        provenance = {
            "model": "remote-model",
            "model_version": "1.0",
            "prompt": "Repair the selected region.",
        }
        payload["allowed_localities"] = ["remote"]
        payload["execution"]["locality"] = "remote"
        payload["generative"].update({
            "used": True,
            "backend_locality": "remote",
            **provenance,
        })
        payload["operation_manifest"]["entry"].update({
            "generative": True,
            **provenance,
        })

        result = adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertIn("untrusted_allowed_localities_override", result["errors"])
        self.assertIn("execution_locality_not_local", result["errors"])
        self.assertIn("generative_backend_locality_not_local", result["errors"])


if __name__ == "__main__":
    unittest.main()
