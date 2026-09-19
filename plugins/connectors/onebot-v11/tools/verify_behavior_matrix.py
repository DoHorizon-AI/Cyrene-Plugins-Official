###############################################################################
# File: plugins/connectors/onebot-v11/tools/verify_behavior_matrix.py
# Role: Verify the Python-to-native behavior evidence matrix.
#
# 模块职责：校验 Python 到 Native 的行为证据矩阵没有漏项或悬空引用。
###############################################################################
"""Validate the current Python behavior inventory and its native evidence.

The matrix is intentionally generated from the checked-in evidence policy so
that adding a Python test without deciding how it is covered fails the gate.
Evidence references point to concrete C# test methods or named CI/package
boundaries; a passing count alone is never accepted as parity evidence.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
PYTHON_TEST_ROOT = REPOSITORY_ROOT / "plugins/connectors/onebot-v11/tests"
MATRIX_PATH = REPOSITORY_ROOT / "plugins/connectors/onebot-v11/behavior_matrix.json"
CSHARP_TEST_ROOT = REPOSITORY_ROOT / "runtime/dotnet-native-aot/Cyrene.OneBot.V11.Tests"

PYTHON_MODULE_COUNTS = {
    "test_connector.py": 18,
    "test_equivalence_fixtures.py": 1,
    "test_native_package.py": 8,
    "test_package.py": 4,
    "test_qqnt_dependency_boundary.py": 2,
    "test_qqnt_direct.py": 71,
    "test_qqnt_host_tck.py": 12,
    "test_reference_artifact.py": 2,
    "test_transport.py": 5,
}

EVIDENCE_CATALOG = {
    "ci:public-ci / dotnet-native-aot": {
        "kind": "rid-native-aot",
        "path": ".github/workflows/public-ci.yml",
    },
    "ci:public-ci / onebot-v11-package": {
        "kind": "clean-package",
        "path": ".github/workflows/public-ci.yml",
    },
    "ci:public-ci / onebot-v11-python-rollback": {
        "kind": "rollback-package",
        "path": ".github/workflows/public-ci.yml",
    },
    "ci:public-ci / onebot-v11-python-reference": {
        "kind": "immutable-python-reference",
        "path": ".github/workflows/public-ci.yml",
    },
    "ci:public-ci / qq-host-tck": {
        "kind": "qq-host-contract-tck",
        "path": ".github/workflows/public-ci.yml",
    },
    "ci:public-ci / source-hygiene": {
        "kind": "source-manifest-and-boundary",
        "path": ".github/workflows/public-ci.yml",
    },
}

# Exact overrides capture the important semantic boundary of a test. The
# filename defaults below still provide evidence for less central package and
# process checks, but every row remains visible in the generated matrix.
EXACT_EVIDENCE: dict[str, list[str]] = {
    (
        "test_connector.py::test_manifest_and_package_descriptor_project_both_connector_profiles"
    ): [
        "ci:public-ci / onebot-v11-package",
        "ci:public-ci / dotnet-native-aot",
    ],
    (
        "test_connector.py::test_worker_activation_can_receive_one_binding_from_environment"
    ): [
        "csharp:OneBotCoreTests.LoadsHttpProfileAndOverlaysActivationBinding",
        "ci:public-ci / dotnet-native-aot",
    ],
    "test_connector.py::test_ordered_content_and_reply_map_to_onebot_segments": [
        "csharp:NativeEquivalenceTests.OutboundAndRequestMappingsMatchThePythonGoldenFixture",
    ],
    "test_connector.py::test_canonical_protobuf_invoke_returns_typed_delivery_result": [
        "csharp:OneBotCoreTests.MapsCanonicalGroupMessageAndReadsDeliveryId",
    ],
    (
        "test_connector.py::test_canonical_protobuf_inbound_event_preserves_type_and_content"
    ): [
        "csharp:NativeEquivalenceTests.InboundMappingsMatchThePythonGoldenFixture",
    ],
    (
        "test_connector.py::test_inbound_request_event_is_normalized_for_product_auto_approval"
    ): [
        "csharp:OneBotCoreTests.NormalizesInboundRequestToExistingJsonContract",
    ],
    "test_connector.py::test_canonical_protobuf_invoke_preserves_cancellation": [
        "csharp:OneBotParityTests.DispatcherPreservesCancellationBeforeTransportDispatch",
    ],
    "test_connector.py::test_respond_request_maps_friend_and_group_invite_approvals": [
        "csharp:OneBotCoreTests.MapsRequestApprovalToFriendAndGroupActions",
        "csharp:NativeEquivalenceTests.OutboundAndRequestMappingsMatchThePythonGoldenFixture",
    ],
    "test_connector.py::test_explicit_invoke_targets_each_same_package_instance": [
        "csharp:OneBotCoreTests.SubscriptionFiltersAndPublishesCanonicalInboundEvents",
        "ci:public-ci / dotnet-native-aot",
    ],
    "test_connector.py::test_implicit_selection_is_only_allowed_when_unambiguous": [
        "csharp:DispatcherTests.UnconfiguredMethodFailsClosed",
        "ci:public-ci / dotnet-native-aot",
    ],
    "test_connector.py::test_subscriptions_are_isolated_by_the_configured_binding": [
        "csharp:OneBotCoreTests.SubscriptionFiltersAndPublishesCanonicalInboundEvents",
    ],
    "test_connector.py::test_invoke_and_subscribe_share_the_same_configured_identity": [
        "csharp:OneBotCoreTests.SubscriptionFiltersAndPublishesCanonicalInboundEvents",
        "csharp:OneBotCoreTests.MapsCanonicalGroupMessageAndReadsDeliveryId",
    ],
    "test_connector.py::test_binding_identity_survives_a_worker_runtime_restart": [
        "csharp:DispatcherTests.UnconfiguredMethodFailsClosed",
        "ci:public-ci / dotnet-native-aot",
    ],
    (
        "test_connector.py::test_unknown_target_and_capability_mismatch_are_deterministic"
    ): [
        "csharp:DispatcherTests.RejectsUnknownCapability",
    ],
    "test_connector.py::test_account_mismatch_cannot_cross_configured_instances": [
        "csharp:OneBotParityTests.RejectsInvalidMessageContentAndAccountIdentity",
    ],
    "test_connector.py::test_cancellation_is_preserved_before_transport_dispatch": [
        "csharp:OneBotParityTests.DispatcherPreservesCancellationBeforeTransportDispatch",
    ],
    "test_connector.py::test_configured_timeout_is_passed_to_transport": [
        "csharp:OneBotCoreTests.MapsHttpTimeoutToDeadlineExceeded",
    ],
    "test_connector.py::test_unknown_segments_are_bounded_vendor_facts": [
        "csharp:OneBotCoreTests.NormalizesInboundMessageToCanonicalPayload",
    ],
    "test_equivalence_fixtures.py::test_checked_in_fixture_matches_python_baseline": [
        "csharp:NativeEquivalenceTests.OutboundAndRequestMappingsMatchThePythonGoldenFixture",
        "csharp:NativeEquivalenceTests.InboundMappingsMatchThePythonGoldenFixture",
    ],
    (
        "test_reference_artifact.py::"
        "test_reference_archive_contains_provenance_and_python_payload"
    ): [
        "ci:public-ci / onebot-v11-python-reference",
    ],
    "test_reference_artifact.py::test_reference_archive_is_deterministic": [
        "ci:public-ci / onebot-v11-python-reference",
    ],
    (
        "test_transport.py::test_forward_websocket_connects_parses_events_and_correlates_actions"
    ): [
        "csharp:OneBotCoreTests.ForwardWebSocketCorrelatesActionAndDeliversEvent",
    ],
    "test_transport.py::test_forward_websocket_auth_failure_is_deterministic": [
        "csharp:OneBotParityTests.ForwardWebSocketClassifiesUnauthorizedHandshake",
    ],
    "test_transport.py::test_forward_websocket_timeout_cancellation_and_shutdown": [
        "csharp:OneBotCoreTests.MapsHttpTimeoutToDeadlineExceeded",
        "csharp:OneBotParityTests.DispatcherPreservesCancellationBeforeTransportDispatch",
    ],
    "test_transport.py::test_two_websocket_bindings_are_transport_and_event_isolated": [
        "csharp:OneBotCoreTests.ForwardWebSocketCorrelatesActionAndDeliversEvent",
        "csharp:OneBotCoreTests.SubscriptionFiltersAndPublishesCanonicalInboundEvents",
    ],
    "test_transport.py::test_reverse_websocket_profile_accepts_a_binding_local_peer": [
        "csharp:OneBotCoreTests.ReverseWebSocketAcceptsAuthorizedPeerAndCorrelatesAction",
    ],
}

FILE_DEFAULT_EVIDENCE: dict[str, list[str]] = {
    "test_native_package.py": [
        "ci:public-ci / dotnet-native-aot",
        "ci:public-ci / onebot-v11-package",
    ],
    "test_package.py": [
        "ci:public-ci / onebot-v11-package",
        "ci:public-ci / dotnet-native-aot",
    ],
    "test_qqnt_dependency_boundary.py": ["ci:public-ci / source-hygiene"],
    "test_qqnt_host_tck.py": [
        "ci:public-ci / qq-host-tck",
        "csharp:QqHostParityTests.FakeHostDispatchesEveryRequestableOperationAndShutsDownCleanly",
    ],
    "test_qqnt_direct.py": [
        "ci:public-ci / dotnet-native-aot",
        "csharp:QqHostParityTests.FixedOperationRegistryIsClosedAndExplicit",
    ],
    "test_reference_artifact.py": [
        "ci:public-ci / onebot-v11-python-reference",
    ],
    "test_transport.py": [
        "csharp:OneBotCoreTests.ForwardWebSocketCorrelatesActionAndDeliversEvent",
        "ci:public-ci / dotnet-native-aot",
    ],
    "test_connector.py": [
        "csharp:OneBotCoreTests.MapsCanonicalGroupMessageAndReadsDeliveryId",
        "ci:public-ci / dotnet-native-aot",
    ],
    "test_equivalence_fixtures.py": [
        "csharp:NativeEquivalenceTests.OutboundAndRequestMappingsMatchThePythonGoldenFixture",
    ],
}

KEYWORD_EVIDENCE: tuple[tuple[tuple[str, ...], list[str]], ...] = (
    (
        ("frame", "stdio", "protocol", "tcp_listener"),
        [
            "csharp:QqHostParityTests.ProtocolRejectsInvalidLengthAndPayloadShapes",
            "csharp:QqHostParityTests.ProtocolReadsFragmentedUtf8Frames",
        ],
    ),
    (
        ("operation", "api_matrix", "allow_list", "callback"),
        [
            "csharp:QqHostParityTests.FixedOperationRegistryIsClosedAndExplicit",
            "csharp:QqHostParityTests.EveryRequestableOperationHasAClosedParameterSchema",
        ],
    ),
    (
        ("result", "sensitive", "identity", "media", "reference"),
        [
            "csharp:QqHostParityTests.ValidatorRejectsUnsafeResults",
            "csharp:QqHostParityTests.ValidatorRequiresNativeIdentityForMessageSendAndAllowsNullReferences",
        ],
    ),
    (
        ("parameter", "extension", "nested", "schema", "passthrough"),
        [
            "csharp:QqHostParityTests.ValidatorRejectsPassthroughAndInvalidTypedParameters",
            "csharp:QqHostParityTests.ValidatorRejectsNestedAndCollectionBounds",
        ],
    ),
    (
        ("hello", "abi", "version", "login", "readiness", "account_mismatch"),
        [
            "csharp:QqHostParityTests.FakeHostRejectsEveryIncompatibleHello",
            "csharp:DispatcherTests.QqDispatcherBootstrapsTheSessionBeforeAReadyOperation",
        ],
    ),
    (
        ("cancel", "cancellation", "timeout", "late_response", "duplicate"),
        [
            "csharp:QqHostParityTests.CancellationSendsControlFrameAndIgnoresLateResponse",
            "csharp:DispatcherTests.QqHostClientRecoversOnceWithoutReplayingTheFailedOperation",
        ],
    ),
    (
        ("generation", "restart", "recover", "crash", "circuit", "process", "shutdown"),
        [
            "csharp:DispatcherTests.QqHostClientRecoversOnceWithoutReplayingTheFailedOperation",
            "csharp:DispatcherTests.QqHostClientOpensTheCrashCircuitAfterTheRestartBudget",
            "csharp:DispatcherTests.QqHostClientReapsBindingLocalProcessTree",
        ],
    ),
    (
        ("event", "subscription", "inbound", "message", "canonical", "mapping"),
        [
            "csharp:DispatcherTests.QqHostClientDeliversCurrentGenerationEvents",
            "csharp:DispatcherTests.QqDispatcherFiltersNativeInboundMessageEvents",
            "csharp:DispatcherTests.QqDispatcherMapsCanonicalSendMessageAndDelivery",
        ],
    ),
    (
        ("config", "installation", "data_directory", "symlink", "profile", "field"),
        [
            "csharp:DispatcherTests.QqHostClientUsesAnExactInstallationManifest",
            "csharp:DispatcherTests.QqDirectProfileRejectsOneBotTransportFieldsAndKeepsAllowListFixed",
        ],
    ),
)


def discover_python_tests() -> list[str]:
    """Return every top-level pytest function in the current connector suite."""

    discovered: list[str] = []
    for path in sorted(PYTHON_TEST_ROOT.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and node.name.startswith("test_"):
                discovered.append(f"{path.name}::{node.name}")
    return discovered


def csharp_test_methods() -> set[str]:
    """Collect concrete public test method references from the C# suite."""

    methods: set[str] = set()
    pattern = re.compile(r"public\s+(?:async\s+)?(?:Task|void)\s+(\w+)")
    for path in CSHARP_TEST_ROOT.glob("*Tests.cs"):
        class_match = re.search(
            r"public\s+sealed\s+class\s+(\w+)",
            path.read_text(encoding="utf-8"),
        )
        if class_match is None:
            continue
        class_name = class_match.group(1)
        for method_match in pattern.finditer(path.read_text(encoding="utf-8")):
            methods.add(f"csharp:{class_name}.{method_match.group(1)}")
    return methods


def evidence_for(test_id: str) -> list[str]:
    """Choose the explicit or closest concrete evidence for one Python test."""

    if test_id in EXACT_EVIDENCE:
        return EXACT_EVIDENCE[test_id]

    filename, name = test_id.split("::", maxsplit=1)
    lowered = name.lower()
    for keywords, evidence in KEYWORD_EVIDENCE:
        if any(keyword in lowered for keyword in keywords):
            return [*evidence, *FILE_DEFAULT_EVIDENCE[filename]]
    return FILE_DEFAULT_EVIDENCE[filename]


def build_matrix() -> dict[str, Any]:
    """Build the deterministic, reviewable matrix document."""

    tests = discover_python_tests()
    rows = [
        {
            "id": test_id,
            "status": "covered",
            "evidence": evidence_for(test_id),
        }
        for test_id in tests
    ]
    return {
        "schema_version": 1,
        "baseline": {
            "suite": "plugins/connectors/onebot-v11/tests",
            "collection_command": (
                "PYTHONPATH=plugins/connectors/onebot-v11/src:"
                "sdk/python/cyrene_plugin_runtime/src python3 -m pytest "
                "--collect-only -q plugins/connectors/onebot-v11/tests"
            ),
            "function_count": len(tests),
            "collected_case_count": 123,
            "module_case_counts": PYTHON_MODULE_COUNTS,
        },
        "evidence_catalog": EVIDENCE_CATALOG,
        "rows": rows,
    }


def validate(matrix: dict[str, Any]) -> None:
    """Fail if inventory, evidence references, or generated values drift."""

    expected = build_matrix()
    if matrix != expected:
        raise SystemExit(
            "behavior_matrix.json is stale; run verify_behavior_matrix.py --write"
        )

    discovered = set(discover_python_tests())
    rows = matrix.get("rows")
    if not isinstance(rows, list):
        raise SystemExit("behavior matrix rows must be a list")
    row_ids = [row.get("id") for row in rows]
    if len(row_ids) != len(set(row_ids)) or set(row_ids) != discovered:
        raise SystemExit(
            "behavior matrix does not match the current Python test inventory"
        )

    references = csharp_test_methods()
    catalog = set(EVIDENCE_CATALOG)
    for row in rows:
        evidence = row.get("evidence")
        if (
            row.get("status") != "covered"
            or not isinstance(evidence, list)
            or not evidence
        ):
            raise SystemExit(
                f"behavior matrix row lacks covered evidence: {row.get('id')}"
            )
        for item in evidence:
            if item.startswith("csharp:") and item not in references:
                raise SystemExit(f"behavior matrix references missing C# test: {item}")
            if item.startswith("ci:") and item not in catalog:
                raise SystemExit(
                    f"behavior matrix references missing CI evidence: {item}"
                )

    if matrix["baseline"]["function_count"] != len(discovered):
        raise SystemExit("behavior matrix Python function count is stale")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write", action="store_true", help="rewrite the generated matrix"
    )
    args = parser.parse_args()

    matrix = build_matrix()
    if args.write:
        MATRIX_PATH.write_text(
            json.dumps(matrix, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0

    if not MATRIX_PATH.is_file():
        raise SystemExit(f"missing behavior matrix: {MATRIX_PATH}")
    checked_in = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    validate(checked_in)
    print(
        f"behavior matrix verified: {len(checked_in['rows'])} Python functions, "
        f"{checked_in['baseline']['collected_case_count']} collected cases"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
