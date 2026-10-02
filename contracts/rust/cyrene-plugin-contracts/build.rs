use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR")?);
    let proto_root = manifest_dir.join("../../proto");
    let direct_plugin_runtime =
        proto_root.join("cyrene/plugin/runtime/v1/direct_plugin_runtime.proto");
    let model_provider = proto_root.join("cyrene/model/provider/v1/model_provider.proto");
    let message_connector = proto_root.join("cyrene/message/connector/v1/message_connector.proto");
    let agent_runtime = proto_root.join("cyrene/agent/runtime/v1/agent_runtime.proto");
    let memory_provider = proto_root.join("cyrene/memory/provider/v1/memory_provider.proto");
    let computer_runtime = proto_root.join("cyrene/computer/runtime/v1/computer_runtime.proto");
    let tool_provider = proto_root.join("cyrene/tool/provider/v1/tool_provider.proto");
    let workspace_authority =
        proto_root.join("cyrene/workspace/authority/v1/workspace_authority.proto");
    let workspace_authority_v2 =
        proto_root.join("cyrene/workspace/authority/v2/workspace_authority.proto");
    let workspace_bridge =
        proto_root.join("cyrene/workspace/bridge/v1/workspace_frontend_bridge.proto");
    let workspace_sidecar = proto_root.join("cyrene/workspace/local/v1/workspace_sidecar.proto");
    let workspace_sidecar_v2 =
        proto_root.join("cyrene/workspace/local/v2/workspace_sidecar.proto");
    let workspace_relay = proto_root.join("cyrene/workspace/relay/v1/workspace_relay.proto");
    let workspace_tunnel = proto_root.join("cyrene/workspace/tunnel/v1/workspace_tunnel.proto");
    let workspace_fabric_legacy = proto_root.join("cyrene/workspace/v1/workspace_fabric.proto");
    let semantic_identity = proto_root.join("cyrene/semantic/v1/identity.proto");
    let product_api = proto_root.join("cyrene/workspace/product/v2/product_api.proto");
    let google_status = proto_root.join("google/rpc/status.proto");

    println!("cargo:rerun-if-changed={}", direct_plugin_runtime.display());
    println!("cargo:rerun-if-changed={}", model_provider.display());
    println!("cargo:rerun-if-changed={}", message_connector.display());
    println!("cargo:rerun-if-changed={}", agent_runtime.display());
    println!("cargo:rerun-if-changed={}", memory_provider.display());
    println!("cargo:rerun-if-changed={}", computer_runtime.display());
    println!("cargo:rerun-if-changed={}", tool_provider.display());
    println!("cargo:rerun-if-changed={}", workspace_authority.display());
    println!("cargo:rerun-if-changed={}", workspace_authority_v2.display());
    println!("cargo:rerun-if-changed={}", workspace_bridge.display());
    println!("cargo:rerun-if-changed={}", workspace_sidecar.display());
    println!("cargo:rerun-if-changed={}", workspace_sidecar_v2.display());
    println!("cargo:rerun-if-changed={}", workspace_relay.display());
    println!("cargo:rerun-if-changed={}", workspace_tunnel.display());
    println!("cargo:rerun-if-changed={}", workspace_fabric_legacy.display());
    println!("cargo:rerun-if-changed={}", semantic_identity.display());
    println!("cargo:rerun-if-changed={}", product_api.display());
    println!("cargo:rerun-if-changed={}", google_status.display());
    println!("cargo:rerun-if-env-changed=CARGO_FEATURE_SERVER");

    std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    let generate_server = std::env::var_os("CARGO_FEATURE_SERVER").is_some();

    tonic_build::configure()
        .build_client(true)
        .build_server(generate_server)
        .build_transport(false)
        .compile_protos(
            &[
                direct_plugin_runtime,
                model_provider,
                message_connector,
                agent_runtime,
                memory_provider,
                computer_runtime,
                tool_provider,
                workspace_authority,
                workspace_authority_v2,
                workspace_bridge,
                workspace_sidecar,
                workspace_sidecar_v2,
                workspace_relay,
                workspace_tunnel,
                workspace_fabric_legacy,
                semantic_identity,
                product_api,
                google_status,
            ],
            &[proto_root, protoc_bin_vendored::include_path()?],
        )?;

    Ok(())
}
