use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR")?);
    let proto_root = manifest_dir.join("../../../contracts/proto");
    let direct_plugin_runtime =
        proto_root.join("cyrene/plugin/runtime/v1/direct_plugin_runtime.proto");

    println!("cargo:rerun-if-changed={}", direct_plugin_runtime.display());

    std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);

    tonic_build::configure()
        .build_server(true)
        .build_client(true)
        .compile(
            &[direct_plugin_runtime],
            &[proto_root, protoc_bin_vendored::include_path()?],
        )?;

    Ok(())
}
