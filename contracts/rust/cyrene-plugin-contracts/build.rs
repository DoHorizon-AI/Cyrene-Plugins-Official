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

    println!("cargo:rerun-if-changed={}", direct_plugin_runtime.display());
    println!("cargo:rerun-if-changed={}", model_provider.display());
    println!("cargo:rerun-if-changed={}", message_connector.display());
    println!("cargo:rerun-if-changed={}", agent_runtime.display());
    println!("cargo:rerun-if-changed={}", memory_provider.display());
    println!("cargo:rerun-if-changed={}", computer_runtime.display());
    println!("cargo:rerun-if-changed={}", tool_provider.display());

    std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);

    prost_build::Config::new().compile_protos(
        &[
            direct_plugin_runtime,
            model_provider,
            message_connector,
            agent_runtime,
            memory_provider,
            computer_runtime,
            tool_provider,
        ],
        &[proto_root, protoc_bin_vendored::include_path()?],
    )?;

    Ok(())
}
