//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 dispatch_credentials.rs                                          │
//! │  Test: Protected Authority-scope Product credential dispatch.        │
//! │                                                                     │
//! │  测试职责：校验凭据映射、请求头、轮换和重定向隔离。                  │
//! └─────────────────────────────────────────────────────────────────────┘

#![cfg(unix)]

use cyrene_plugin_contracts::workspace_authority_v2::{
    ExecutionCredential, ExecutionTarget, IdempotencySemantics,
};
use cyrene_plugin_contracts::workspace_product_v2::ProductApiInvocationV2;
use cyrene_workspace_product_adapters::{
    GenericProductHttpAdapter, ProductAdapterError, ProtectedCredentialProvider,
};
use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Path;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const TOKEN_ONE: &str = "test-yield-token-0123456789abcdefghijkl";
const TOKEN_TWO: &str = "test-yield-token-9876543210zyxwvutsrqpon";

fn invocation_target_credential(
    endpoint: &str,
) -> (ProductApiInvocationV2, ExecutionTarget, ExecutionCredential) {
    invocation_target_credential_for_resource(endpoint, "")
}

fn invocation_target_credential_for_resource(
    endpoint: &str,
    resource_id: &str,
) -> (ProductApiInvocationV2, ExecutionTarget, ExecutionCredential) {
    let invocation = ProductApiInvocationV2 {
        owner_id: "yield".into(),
        operation_id: "op_training_start".into(),
        json_body: format!(r#"{{"draftId":"{resource_id}"}}"#).into_bytes(),
        resource_id: resource_id.into(),
        idempotency_key: String::new(),
    };
    let target = ExecutionTarget {
        target_component: "yield".into(),
        http_method: "POST".into(),
        endpoint: endpoint.into(),
        resource_id: resource_id.into(),
        scope: "workspace:ws-main:yield:op_training_start".into(),
        idempotency_key: String::new(),
        idempotency_semantics: IdempotencySemantics::NotSupported as i32,
        execution_device_id: "device-main".into(),
        execution_device_generation: 3,
        execution_authorization_id: vec![0x31; 16],
        execution_device_certificate_sha256: vec![0x42; 32],
    };
    let credential = ExecutionCredential {
        invocation_id: "inv-main".into(),
        invocation_digest_sha256: vec![0x11; 32],
        request_digest_sha256: vec![0x22; 32],
        organization_id: "org-main".into(),
        workspace_id: "ws-main".into(),
        principal_issuer: "issuer-test".into(),
        principal_subject: "subject-test".into(),
        operation_owner_id: invocation.owner_id.clone(),
        operation_id: invocation.operation_id.clone(),
        scope: target.scope.clone(),
        resource_id: target.resource_id.clone(),
        target_component: target.target_component.clone(),
        http_method: target.http_method.clone(),
        endpoint: target.endpoint.clone(),
        execution_device_id: target.execution_device_id.clone(),
        execution_device_generation: target.execution_device_generation,
        execution_authorization_id: target.execution_authorization_id.clone(),
        execution_device_certificate_sha256: target.execution_device_certificate_sha256.clone(),
        session_id: "session-test".into(),
        session_generation: 2,
        contract_activation_generation: 7,
        idempotency_key: target.idempotency_key.clone(),
        idempotency_semantics: target.idempotency_semantics,
        signing_key_id: "authority-key-test".into(),
        issued_at: None,
        expires_at: None,
        authority_signature: vec![0x53; 64],
    };
    (invocation, target, credential)
}

fn hex(bytes: &[u8]) -> String {
    let mut result = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write;
        write!(result, "{byte:02x}").unwrap();
    }
    result
}

fn map_row(credential: &ExecutionCredential, token: &str) -> Value {
    map_row_with_resource_constraint(
        credential,
        token,
        json!({ "kind": "authorityApprovedResource" }),
    )
}

fn map_row_with_resource_constraint(
    credential: &ExecutionCredential,
    token: &str,
    resource_constraint: Value,
) -> Value {
    json!({
        "organizationId": credential.organization_id,
        "workspaceId": credential.workspace_id,
        "operationOwnerId": credential.operation_owner_id,
        "operationId": credential.operation_id,
        "scope": credential.scope,
        "resourceConstraint": resource_constraint,
        "targetComponent": credential.target_component,
        "httpMethod": credential.http_method,
        "endpointOrigin": reqwest::Url::parse(&credential.endpoint)
            .unwrap()
            .origin()
            .ascii_serialization(),
        "executionDeviceId": credential.execution_device_id,
        "executionDeviceGeneration": credential.execution_device_generation,
        "executionAuthorizationIdHex": hex(&credential.execution_authorization_id),
        "executionDeviceCertificateSha256Hex": hex(&credential.execution_device_certificate_sha256),
        "contractActivationGeneration": credential.contract_activation_generation,
        "bearerToken": token,
    })
}

fn write_map(path: &Path, rows: Vec<Value>) {
    let bytes = serde_json::to_vec(&json!({ "version": 2, "credentials": rows })).unwrap();
    let mut file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)
        .unwrap();
    file.write_all(&bytes).unwrap();
    file.sync_all().unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
}

fn adapter(path: &Path, allowed_origin: &str) -> GenericProductHttpAdapter {
    GenericProductHttpAdapter::new(
        Duration::from_secs(2),
        vec![allowed_origin.to_string()],
        ProtectedCredentialProvider::new(path).unwrap(),
    )
}

struct CapturedRequest {
    path: String,
    authorization: String,
}

async fn serve_http_once(
    listener: &tokio::net::TcpListener,
    status: &str,
    location: Option<String>,
) -> CapturedRequest {
    let (mut stream, _) = listener.accept().await.unwrap();
    let mut request = Vec::new();
    let mut chunk = [0_u8; 1024];
    loop {
        let count = stream.read(&mut chunk).await.unwrap();
        assert_ne!(count, 0);
        request.extend_from_slice(&chunk[..count]);
        if request.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
        assert!(request.len() < 16 * 1024);
    }
    let request = String::from_utf8_lossy(&request);
    let path = request
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .unwrap_or_default()
        .to_string();
    let authorization = request
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("authorization")
                .then(|| value.trim().to_string())
        })
        .unwrap_or_default();
    let mut response = format!("HTTP/1.1 {status}\r\ncontent-length: 0\r\nconnection: close\r\n");
    if let Some(location) = location {
        response.push_str(&format!("location: {location}\r\n"));
    }
    response.push_str("\r\n");
    stream.write_all(response.as_bytes()).await.unwrap();
    CapturedRequest {
        path,
        authorization,
    }
}

#[tokio::test]
async fn dispatch_sends_only_the_matching_scope_token_and_reloads_after_rotation() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let endpoint =
        format!("http://{address}/internal/workspace/v1/training-drafts/draft-1/actions/start");
    let origin = format!("http://{address}");
    let (invocation, target, credential) = invocation_target_credential(&endpoint);
    let temp = tempfile::tempdir().unwrap();
    let map_path = temp.path().join("workspace-product-credentials.json");
    write_map(&map_path, vec![map_row(&credential, TOKEN_ONE)]);
    let adapter = adapter(&map_path, &origin);

    let server = tokio::spawn(async move {
        let first = serve_http_once(&listener, "200 OK", None).await;
        let second = serve_http_once(&listener, "200 OK", None).await;
        [first, second]
    });

    let first = adapter
        .dispatch(&invocation, &target, &credential, Duration::ZERO)
        .await
        .unwrap();
    assert_eq!(first.status_code, 200);

    let rotated_path = temp.path().join("rotated.json");
    write_map(&rotated_path, vec![map_row(&credential, TOKEN_TWO)]);
    fs::rename(&rotated_path, &map_path).unwrap();
    let second = adapter
        .dispatch(&invocation, &target, &credential, Duration::ZERO)
        .await
        .unwrap();
    assert_eq!(second.status_code, 200);

    let headers = server.await.unwrap();
    assert!(headers[0].authorization == format!("Bearer {TOKEN_ONE}"));
    assert!(headers[1].authorization == format!("Bearer {TOKEN_TWO}"));
}

#[tokio::test]
async fn authority_approved_resource_constraint_covers_distinct_resolved_ids_and_paths() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let origin = format!("http://{address}");
    let endpoint_one = format!("{origin}/internal/training-drafts/draft-one/actions/start");
    let endpoint_two = format!("{origin}/internal/training-drafts/draft-two/actions/start");
    let (invocation_one, target_one, credential_one) =
        invocation_target_credential_for_resource(&endpoint_one, "draft-one");
    let (invocation_two, target_two, credential_two) =
        invocation_target_credential_for_resource(&endpoint_two, "draft-two");

    let temp = tempfile::tempdir().unwrap();
    let map_path = temp.path().join("credentials.json");
    write_map(&map_path, vec![map_row(&credential_one, TOKEN_ONE)]);
    let adapter = adapter(&map_path, &origin);

    let server = tokio::spawn(async move {
        [
            serve_http_once(&listener, "200 OK", None).await,
            serve_http_once(&listener, "200 OK", None).await,
        ]
    });
    let first = adapter
        .dispatch(
            &invocation_one,
            &target_one,
            &credential_one,
            Duration::ZERO,
        )
        .await
        .unwrap();
    let second = adapter
        .dispatch(
            &invocation_two,
            &target_two,
            &credential_two,
            Duration::ZERO,
        )
        .await
        .unwrap();
    assert_eq!(first.status_code, 200);
    assert_eq!(second.status_code, 200);

    let captured = server.await.unwrap();
    assert!(captured[0].path == "/internal/training-drafts/draft-one/actions/start");
    assert!(captured[1].path == "/internal/training-drafts/draft-two/actions/start");
    assert!(captured[0].authorization == format!("Bearer {TOKEN_ONE}"));
    assert!(captured[1].authorization == format!("Bearer {TOKEN_ONE}"));
}

#[tokio::test]
async fn exact_resource_constraint_only_matches_its_configured_id() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let origin = format!("http://{address}");
    let endpoint_one = format!("{origin}/internal/training-drafts/draft-one/actions/start");
    let endpoint_two = format!("{origin}/internal/training-drafts/draft-two/actions/start");
    let (invocation_one, target_one, credential_one) =
        invocation_target_credential_for_resource(&endpoint_one, "draft-one");
    let (invocation_two, target_two, credential_two) =
        invocation_target_credential_for_resource(&endpoint_two, "draft-two");

    let temp = tempfile::tempdir().unwrap();
    let map_path = temp.path().join("credentials.json");
    write_map(
        &map_path,
        vec![map_row_with_resource_constraint(
            &credential_one,
            TOKEN_ONE,
            json!({ "kind": "exact", "resourceId": "draft-one" }),
        )],
    );
    let adapter = adapter(&map_path, &origin);

    let server = tokio::spawn(async move { serve_http_once(&listener, "200 OK", None).await });
    let first = adapter
        .dispatch(
            &invocation_one,
            &target_one,
            &credential_one,
            Duration::ZERO,
        )
        .await
        .unwrap();
    assert_eq!(first.status_code, 200);
    let second = adapter
        .dispatch(
            &invocation_two,
            &target_two,
            &credential_two,
            Duration::ZERO,
        )
        .await;
    assert!(matches!(
        second,
        Err(ProductAdapterError::InvalidInvocation(_))
    ));

    let captured = server.await.unwrap();
    assert!(captured.path == "/internal/training-drafts/draft-one/actions/start");
    assert!(captured.authorization == format!("Bearer {TOKEN_ONE}"));
}

#[tokio::test]
async fn workspace_and_target_mismatches_are_rejected_before_network_dispatch() {
    let endpoint = "http://127.0.0.1:9/approved";
    let origin = "http://127.0.0.1:9";
    let (invocation, target, credential) = invocation_target_credential(endpoint);
    let temp = tempfile::tempdir().unwrap();
    let map_path = temp.path().join("credentials.json");
    write_map(&map_path, vec![map_row(&credential, TOKEN_ONE)]);
    let adapter = adapter(&map_path, origin);

    let mut cross_workspace = credential.clone();
    cross_workspace.workspace_id = "ws-other".into();
    let workspace_result = adapter
        .dispatch(&invocation, &target, &cross_workspace, Duration::ZERO)
        .await;
    let workspace_error = match workspace_result {
        Err(error @ ProductAdapterError::InvalidInvocation(_)) => error,
        _ => panic!("cross-workspace credential was not denied"),
    };
    assert!(!format!("{workspace_error:?}").contains(TOKEN_ONE));

    let mut wrong_operation = credential.clone();
    wrong_operation.operation_id = "op_training_cancel".into();
    assert!(matches!(
        adapter
            .dispatch(&invocation, &target, &wrong_operation, Duration::ZERO)
            .await,
        Err(ProductAdapterError::InvalidInvocation(_))
    ));

    let mut wrong_resource = credential.clone();
    wrong_resource.resource_id = "draft-other".into();
    assert!(matches!(
        adapter
            .dispatch(&invocation, &target, &wrong_resource, Duration::ZERO)
            .await,
        Err(ProductAdapterError::InvalidInvocation(_))
    ));

    let mut wrong_endpoint = target.clone();
    wrong_endpoint.endpoint.push_str("/different-resource");
    assert!(matches!(
        adapter
            .dispatch(&invocation, &wrong_endpoint, &credential, Duration::ZERO)
            .await,
        Err(ProductAdapterError::InvalidInvocation(_))
    ));

    let mut wrong_method = target.clone();
    wrong_method.http_method = "GET".into();
    assert!(matches!(
        adapter
            .dispatch(&invocation, &wrong_method, &credential, Duration::ZERO)
            .await,
        Err(ProductAdapterError::InvalidInvocation(_))
    ));

    let mut wrong_map_target = map_row(&credential, TOKEN_ONE);
    wrong_map_target["targetComponent"] = json!("other-product");
    write_map(&map_path, vec![wrong_map_target]);
    let target_result = adapter
        .dispatch(&invocation, &target, &credential, Duration::ZERO)
        .await;
    assert!(matches!(
        target_result,
        Err(ProductAdapterError::InvalidInvocation(_))
    ));

    let mut wrong_map_endpoint = map_row(&credential, TOKEN_ONE);
    wrong_map_endpoint["endpointOrigin"] = json!("http://127.0.0.1:10");
    write_map(&map_path, vec![wrong_map_endpoint]);
    let endpoint_result = adapter
        .dispatch(&invocation, &target, &credential, Duration::ZERO)
        .await;
    assert!(matches!(
        endpoint_result,
        Err(ProductAdapterError::InvalidInvocation(_))
    ));
}

#[test]
fn duplicate_invalid_and_unsafe_scope_maps_are_rejected_without_echoing_tokens() {
    let (_, _, credential) = invocation_target_credential("https://yield.example.test/start");
    let temp = tempfile::tempdir().unwrap();
    let map_path = temp.path().join("credentials.json");

    write_map(
        &map_path,
        vec![
            map_row(&credential, TOKEN_ONE),
            map_row(&credential, TOKEN_TWO),
        ],
    );
    assert!(ProtectedCredentialProvider::new(&map_path).is_err());

    let mut invalid_constraint = map_row(&credential, TOKEN_ONE);
    invalid_constraint["resourceConstraint"] = json!({ "kind": "untrustedWildcard" });
    write_map(&map_path, vec![invalid_constraint]);
    assert!(ProtectedCredentialProvider::new(&map_path).is_err());

    let mut noncanonical_origin = map_row(&credential, TOKEN_ONE);
    noncanonical_origin["endpointOrigin"] = json!("https://yield.example.test/path");
    write_map(&map_path, vec![noncanonical_origin]);
    assert!(ProtectedCredentialProvider::new(&map_path).is_err());

    let mut another_scope = credential.clone();
    another_scope.workspace_id = "ws-other".into();
    write_map(
        &map_path,
        vec![
            map_row(&credential, TOKEN_ONE),
            map_row(&another_scope, TOKEN_ONE),
        ],
    );
    assert!(ProtectedCredentialProvider::new(&map_path).is_err());

    for invalid_token in ["short", "newline-token-012345678901234567\n"] {
        write_map(&map_path, vec![map_row(&credential, invalid_token)]);
        let error = match ProtectedCredentialProvider::new(&map_path) {
            Ok(_) => panic!("invalid token accepted"),
            Err(error) => error,
        };
        assert!(!format!("{error:?}").contains(invalid_token));
    }

    write_map(&map_path, vec![map_row(&credential, TOKEN_ONE)]);
    for mode in [0o200, 0o100, 0o440, 0o444, 0o640, 0o644, 0o700, 0o1400] {
        fs::set_permissions(&map_path, fs::Permissions::from_mode(mode)).unwrap();
        assert!(
            ProtectedCredentialProvider::new(&map_path).is_err(),
            "accepted unsafe permission mode {mode:o}"
        );
    }

    fs::set_permissions(&map_path, fs::Permissions::from_mode(0o400)).unwrap();
    assert!(ProtectedCredentialProvider::new(&map_path).is_ok());

    fs::set_permissions(&map_path, fs::Permissions::from_mode(0o600)).unwrap();
    let linked_path = temp.path().join("hardlink.json");
    fs::hard_link(&map_path, &linked_path).unwrap();
    assert!(ProtectedCredentialProvider::new(&linked_path).is_err());

    let symlink_path = temp.path().join("symlink.json");
    std::os::unix::fs::symlink(&map_path, &symlink_path).unwrap();
    assert!(ProtectedCredentialProvider::new(&symlink_path).is_err());
}

#[test]
fn exact_resource_rows_can_coexist_but_overlapping_constraints_are_rejected() {
    let (_, _, credential) = invocation_target_credential("https://yield.example.test/start");
    let temp = tempfile::tempdir().unwrap();
    let map_path = temp.path().join("credentials.json");
    let exact_one = map_row_with_resource_constraint(
        &credential,
        TOKEN_ONE,
        json!({ "kind": "exact", "resourceId": "draft-one" }),
    );
    let exact_two = map_row_with_resource_constraint(
        &credential,
        TOKEN_TWO,
        json!({ "kind": "exact", "resourceId": "draft-two" }),
    );

    write_map(&map_path, vec![exact_one.clone(), exact_two]);
    assert!(ProtectedCredentialProvider::new(&map_path).is_ok());

    let duplicate_exact = map_row_with_resource_constraint(
        &credential,
        TOKEN_TWO,
        json!({ "kind": "exact", "resourceId": "draft-one" }),
    );
    write_map(&map_path, vec![exact_one.clone(), duplicate_exact]);
    assert!(ProtectedCredentialProvider::new(&map_path).is_err());

    write_map(&map_path, vec![exact_one, map_row(&credential, TOKEN_TWO)]);
    assert!(ProtectedCredentialProvider::new(&map_path).is_err());
}

#[test]
fn owner_and_group_mismatches_are_rejected_when_the_test_process_can_set_them() {
    use rustix::fs::chown;
    use rustix::process::{getegid, geteuid, Gid, Uid};

    let (_, _, credential) = invocation_target_credential("https://yield.example.test/start");
    let temp = tempfile::tempdir().unwrap();

    let owner_path = temp.path().join("wrong-owner.json");
    write_map(&owner_path, vec![map_row(&credential, TOKEN_ONE)]);
    let current_uid = geteuid().as_raw();
    let other_uid = Uid::from_raw(if current_uid == 0 { 1 } else { 0 });
    if chown(&owner_path, Some(other_uid), None).is_ok() {
        assert!(ProtectedCredentialProvider::new(&owner_path).is_err());
    }

    let group_path = temp.path().join("wrong-group.json");
    write_map(&group_path, vec![map_row(&credential, TOKEN_ONE)]);
    let current_gid = getegid().as_raw();
    let other_gid = Gid::from_raw(if current_gid == 1 { 2 } else { 1 });
    if chown(&group_path, None, Some(other_gid)).is_ok() {
        assert!(ProtectedCredentialProvider::new(&group_path).is_err());
    }

    let systemd_credential_path = temp.path().join("systemd-credential.json");
    write_map(
        &systemd_credential_path,
        vec![map_row(&credential, TOKEN_ONE)],
    );
    if chown(&systemd_credential_path, None, Some(Gid::from_raw(0))).is_ok() {
        fs::set_permissions(&systemd_credential_path, fs::Permissions::from_mode(0o400)).unwrap();
        assert!(ProtectedCredentialProvider::new(&systemd_credential_path).is_ok());
    }
}

#[tokio::test]
async fn authorization_header_is_not_forwarded_through_redirects() {
    let first_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let first_address = first_listener.local_addr().unwrap();
    let second_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let second_address = second_listener.local_addr().unwrap();
    let endpoint = format!("http://{first_address}/approved");
    let origin = format!("http://{first_address}");
    let redirect_target = format!("http://{second_address}/collect");
    let (invocation, target, credential) = invocation_target_credential(&endpoint);
    let temp = tempfile::tempdir().unwrap();
    let map_path = temp.path().join("credentials.json");
    write_map(&map_path, vec![map_row(&credential, TOKEN_ONE)]);
    let adapter = adapter(&map_path, &origin);

    let first_server = tokio::spawn(async move {
        serve_http_once(&first_listener, "302 Found", Some(redirect_target)).await
    });
    let response = adapter
        .dispatch(&invocation, &target, &credential, Duration::ZERO)
        .await
        .unwrap();
    assert_eq!(response.status_code, 302);
    assert!(first_server.await.unwrap().authorization == format!("Bearer {TOKEN_ONE}"));
    assert!(
        tokio::time::timeout(Duration::from_millis(250), second_listener.accept())
            .await
            .is_err()
    );
}
