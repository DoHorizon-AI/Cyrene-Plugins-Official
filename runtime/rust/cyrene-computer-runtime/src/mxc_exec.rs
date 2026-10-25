//! Trusted launcher for one Microsoft MXC Bubblewrap workload.
//!
//! This binary is installed beside the Computer Runtime server. It accepts a
//! private host control file, launches only the pinned MXC SDK v1 backend, and
//! forwards only workload stdout and stderr to its parent.

#[cfg(target_os = "linux")]
mod linux {
    use cyrene_computer_runtime::sandbox::mxc_protocol::{
        ensure_control_directories_outside_workspaces, trusted_control_directories,
        trusted_control_path, MxcControlRequest, MxcTerminalReceipt, MAX_MXC_CONTROL_REQUEST_BYTES,
        MAX_MXC_RECEIPT_MESSAGE_BYTES, MAX_MXC_RECEIPT_WARNINGS, MXC_CONTROL_SCHEMA_VERSION,
    };
    use mxc_sdk::v1::{
        self, ContainerRequest, Containment, FilesystemPolicy, NetworkAction, NetworkEgressPolicy,
        NetworkIngressPolicy, NetworkPolicy, SpawnOptions, WaitResult,
    };
    use serde::de::DeserializeOwned;
    use std::fs::{self, File, OpenOptions};
    use std::io::{self, Read, Write};
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use std::thread;
    use std::time::{Duration, Instant};

    const MAX_COMMAND_BYTES: usize = 512 * 1024;
    const MAX_ENV_ENTRIES: usize = 128;
    const MAX_ENV_VALUE_BYTES: usize = 16 * 1024;
    const STDERR_ERROR_EXIT: i32 = 70;
    const DEFAULT_WORKLOAD_PATH: &str =
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";
    const MINIMUM_BUBBLEWRAP_VERSION: (u32, u32, u32) = (0, 12, 0);
    const BUBBLEWRAP_VERSION_TIMEOUT: Duration = Duration::from_secs(2);
    const BUBBLEWRAP_VERSION_OUTPUT_LIMIT: usize = 1024;

    struct Paths {
        request: PathBuf,
        receipt: PathBuf,
    }

    enum HelperMode {
        Execute(Paths),
        Probe(Vec<PathBuf>),
    }

    fn bounded_message(value: impl AsRef<str>) -> String {
        bounded_text(value.as_ref(), MAX_MXC_RECEIPT_MESSAGE_BYTES)
    }

    fn bounded_warnings(warnings: Vec<String>) -> Vec<String> {
        warnings
            .into_iter()
            .take(MAX_MXC_RECEIPT_WARNINGS)
            .map(|warning| bounded_text(&warning, 128))
            .collect()
    }

    fn bounded_text(value: &str, max_bytes: usize) -> String {
        let normalized = value
            .chars()
            .map(|character| {
                if character.is_control() {
                    ' '
                } else {
                    character
                }
            })
            .collect::<String>();
        let mut end = normalized.len().min(max_bytes);
        while !normalized.is_char_boundary(end) {
            end -= 1;
        }
        normalized[..end].to_string()
    }

    fn parse_mode(args: impl Iterator<Item = String>) -> Result<HelperMode, String> {
        let mut args = args;
        let first = args
            .next()
            .ok_or_else(|| "MXC helper requires an operation".to_string())?;
        if first == "--probe" {
            let mut roots = Vec::new();
            while let Some(argument) = args.next() {
                if argument != "--workspace-root" {
                    return Err("invalid MXC probe arguments".into());
                }
                let root = args
                    .next()
                    .ok_or_else(|| "MXC probe is missing a workspace root".to_string())?;
                roots.push(PathBuf::from(root));
            }
            if roots.is_empty() {
                return Err("MXC probe requires a configured workspace root".into());
            }
            return Ok(HelperMode::Probe(roots));
        }
        if first != "--execute" {
            return Err("invalid MXC helper operation".into());
        }
        let mut request = None;
        let mut receipt = None;
        while let Some(argument) = args.next() {
            match argument.as_str() {
                "--request" if request.is_none() => {
                    request = args.next().map(PathBuf::from);
                }
                "--receipt" if receipt.is_none() => {
                    receipt = args.next().map(PathBuf::from);
                }
                _ => return Err("invalid MXC helper arguments".into()),
            }
        }
        match (request, receipt) {
            (Some(request), Some(receipt)) if request != receipt => {
                Ok(HelperMode::Execute(Paths { request, receipt }))
            }
            _ => Err("MXC helper requires distinct --request and --receipt paths".into()),
        }
    }

    fn read_private_json<T: DeserializeOwned>(path: &Path, max_bytes: usize) -> Result<T, String> {
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
            .open(path)
            .map_err(|error| format!("cannot open private MXC control file: {error}"))?;
        let metadata = file
            .metadata()
            .map_err(|error| format!("cannot inspect private MXC control file: {error}"))?;
        if !metadata.is_file()
            // SAFETY: geteuid has no pointer arguments and performs no mutation.
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.mode() & 0o077 != 0
            || metadata.nlink() != 1
        {
            return Err("MXC control file ownership or permissions are invalid".into());
        }

        let mut bytes = Vec::with_capacity(metadata.len().min(max_bytes as u64) as usize);
        file.take(max_bytes as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|error| format!("cannot read private MXC control file: {error}"))?;
        if bytes.len() > max_bytes {
            return Err("MXC control file exceeds its size limit".into());
        }
        serde_json::from_slice(&bytes)
            .map_err(|error| format!("invalid private MXC control JSON: {error}"))
    }

    fn validate_request(request: &mut MxcControlRequest, control_dir: &Path) -> Result<(), String> {
        if request.schema_version != MXC_CONTROL_SCHEMA_VERSION {
            return Err("unsupported MXC control schema version".into());
        }
        if request.timeout_ms == 0 {
            return Err("MXC workload timeout must be a positive number of milliseconds".into());
        }
        if request.command.trim().is_empty()
            || request.command.len() > MAX_COMMAND_BYTES
            || request.command.contains('\0')
        {
            return Err("MXC workload command is empty, oversized, or contains NUL".into());
        }
        if !valid_start_marker(&request.start_marker) {
            return Err("MXC workload start marker is invalid".into());
        }
        if request.workspace_roots.is_empty() || request.environment.len() > MAX_ENV_ENTRIES {
            return Err("MXC workload policy has no roots or too many environment entries".into());
        }

        let control_dir = control_dir
            .canonicalize()
            .map_err(|error| format!("cannot resolve MXC control directory: {error}"))?;
        let mut roots = Vec::with_capacity(request.workspace_roots.len());
        for root in &request.workspace_roots {
            let path = Path::new(root)
                .canonicalize()
                .map_err(|error| format!("cannot resolve configured MXC workspace: {error}"))?;
            if !path.is_dir() || !path.is_absolute() || control_dir.starts_with(&path) {
                return Err("MXC workspace overlaps the private control directory".into());
            }
            if !roots.contains(&path) {
                roots.push(path);
            }
        }
        let executable = std::env::current_exe()
            .map_err(|error| format!("cannot resolve MXC helper executable: {error}"))?;
        let control_directories = trusted_control_directories(&executable)?;
        ensure_control_directories_outside_workspaces(&control_directories, &roots)?;
        let working_directory = Path::new(&request.working_directory)
            .canonicalize()
            .map_err(|error| format!("cannot resolve MXC working directory: {error}"))?;
        if !working_directory.is_dir()
            || !roots.iter().any(|root| working_directory.starts_with(root))
        {
            return Err("MXC working directory is outside configured workspaces".into());
        }
        request.workspace_roots = roots
            .iter()
            .map(|path| {
                path.to_str()
                    .map(str::to_string)
                    .ok_or_else(|| "MXC workspace path is not valid UTF-8".to_string())
            })
            .collect::<Result<Vec<_>, _>>()?;
        request.working_directory = working_directory
            .to_str()
            .ok_or_else(|| "MXC working directory is not valid UTF-8".to_string())?
            .to_string();

        for (key, value) in &request.environment {
            if !valid_env_key(key)
                || value.contains('\0')
                || value.len() > MAX_ENV_VALUE_BYTES
                || is_sensitive_env_key(key)
            {
                return Err(format!("MXC workload environment entry '{key}' is invalid"));
            }
        }
        request
            .environment
            .insert("HOME".into(), "/tmp/cyrene-home".into());
        request.environment.insert("TMPDIR".into(), "/tmp".into());
        request
            .environment
            .entry("PATH".into())
            .or_insert_with(|| DEFAULT_WORKLOAD_PATH.into());
        Ok(())
    }

    fn valid_env_key(key: &str) -> bool {
        let mut bytes = key.bytes();
        matches!(bytes.next(), Some(b'A'..=b'Z' | b'a'..=b'z' | b'_'))
            && bytes.all(|byte| matches!(byte, b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_'))
    }

    fn is_sensitive_env_key(key: &str) -> bool {
        let upper = key.to_ascii_uppercase();
        [
            "API_KEY",
            "TOKEN",
            "SECRET",
            "PASSWORD",
            "PASSWD",
            "CREDENTIAL",
            "PRIVATE",
            "AUTH",
            "COOKIE",
            "BEARER",
            "SIGNATURE",
            "SESSION",
        ]
        .iter()
        .any(|pattern| upper.contains(pattern))
    }

    fn posix_quote(value: &str) -> String {
        let mut quoted = String::with_capacity(value.len() + 2);
        quoted.push('\'');
        for character in value.chars() {
            if character == '\'' {
                quoted.push_str("'\\''");
            } else {
                quoted.push(character);
            }
        }
        quoted.push('\'');
        quoted
    }

    fn valid_start_marker(marker: &str) -> bool {
        marker
            .strip_prefix("CYRENE_MXC_STARTED_")
            .is_some_and(|nonce| {
                nonce.len() == 32 && nonce.bytes().all(|byte| byte.is_ascii_hexdigit())
            })
    }

    fn random_start_marker() -> Result<String, String> {
        let mut nonce = [0u8; 16];
        File::open("/dev/urandom")
            .and_then(|mut random| random.read_exact(&mut nonce))
            .map_err(|error| format!("cannot generate an MXC start nonce: {error}"))?;
        Ok(format!("CYRENE_MXC_STARTED_{}", hex(&nonce)))
    }

    fn hex(bytes: &[u8]) -> String {
        const DIGITS: &[u8; 16] = b"0123456789abcdef";
        let mut output = String::with_capacity(bytes.len() * 2);
        for byte in bytes {
            output.push(DIGITS[(byte >> 4) as usize] as char);
            output.push(DIGITS[(byte & 0x0f) as usize] as char);
        }
        output
    }

    fn wrapped_command(command: &str, start_marker: &str) -> String {
        format!(
            "mkdir -p -- /tmp/cyrene-home && printf '%s\\n' {} && exec /bin/bash -c {}",
            posix_quote(start_marker),
            posix_quote(command),
        )
    }

    fn consume_start_marker(stdout: &mut dyn Read, expected: &str) -> Result<(), String> {
        let mut actual = vec![0u8; expected.len() + 1];
        stdout
            .read_exact(&mut actual)
            .map_err(|error| format!("MXC workload start marker was not produced: {error}"))?;
        let mut expected_line = expected.as_bytes().to_vec();
        expected_line.push(b'\n');
        if actual != expected_line {
            return Err("MXC workload start marker did not match the private nonce".into());
        }
        Ok(())
    }

    fn bubblewrap_request(
        command: String,
        workspace_roots: Vec<String>,
        working_directory: String,
        environment: Vec<(String, String)>,
        timeout_ms: u32,
    ) -> ContainerRequest {
        let mut request = ContainerRequest::new(command);
        request.containment = Containment::Bubblewrap;
        request.filesystem = Some(FilesystemPolicy {
            readwrite_paths: workspace_roots,
            ..Default::default()
        });
        request.network = Some(NetworkPolicy {
            egress: Some(NetworkEgressPolicy {
                default: Some(NetworkAction::Deny),
                ..Default::default()
            }),
            ingress: Some(NetworkIngressPolicy {
                default: Some(NetworkAction::Deny),
                host_loopback: Some(NetworkAction::Deny),
            }),
            runtime_config: None,
        });
        request.working_directory = Some(working_directory);
        request.environment = Some(environment);
        request.inherit_default_environment = Some(false);
        request.timeout_ms = Some(timeout_ms);
        request
    }

    fn fixed_control_path() -> Result<String, String> {
        let executable = std::env::current_exe()
            .map_err(|error| format!("cannot resolve MXC helper executable: {error}"))?;
        let directories = trusted_control_directories(&executable)?;
        trusted_control_path(&directories)
    }

    fn verify_bubblewrap_version() -> Result<(u32, u32, u32), String> {
        use std::os::unix::process::CommandExt;

        let executable = std::env::current_exe()
            .map_err(|error| format!("cannot resolve MXC helper executable: {error}"))?;
        let directories = trusted_control_directories(&executable)?;
        let control_path = trusted_control_path(&directories)?;
        std::env::set_var("PATH", &control_path);

        let mut bubblewrap = None;
        for directory in directories {
            let candidate = directory.join("bwrap");
            match std::fs::symlink_metadata(&candidate) {
                Ok(metadata) => {
                    if !metadata.file_type().is_file() || metadata.mode() & 0o111 == 0 {
                        return Err(format!(
                            "trusted Bubblewrap path '{}' is not a regular executable",
                            candidate.display()
                        ));
                    }
                    bubblewrap = Some(candidate);
                    break;
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => {
                    return Err(format!(
                        "cannot inspect trusted Bubblewrap candidate '{}': {error}",
                        candidate.display()
                    ));
                }
            }
        }
        let bubblewrap = bubblewrap.ok_or_else(|| {
            "no Bubblewrap executable is available on the fixed MXC control PATH".to_string()
        })?;

        let mut command = std::process::Command::new(&bubblewrap);
        command
            .arg("--version")
            .env_clear()
            .env("PATH", &control_path)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .process_group(0);
        let mut child = command
            .spawn()
            .map_err(|error| format!("cannot check trusted Bubblewrap version: {error}"))?;
        let child_id = child.id();
        let mut process_group = ProbeProcessGroupGuard(Some(child_id));
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "Bubblewrap version stdout is unavailable".to_string())?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| "Bubblewrap version stderr is unavailable".to_string())?;
        let stdout_reader =
            thread::spawn(move || read_bounded(stdout, BUBBLEWRAP_VERSION_OUTPUT_LIMIT));
        let stderr_reader =
            thread::spawn(move || read_bounded(stderr, BUBBLEWRAP_VERSION_OUTPUT_LIMIT));
        let started = Instant::now();
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => {
                    process_group.disarm();
                    break Some(status);
                }
                Ok(None) if started.elapsed() < BUBBLEWRAP_VERSION_TIMEOUT => {
                    thread::sleep(Duration::from_millis(10));
                }
                Ok(None) => {
                    kill_process_group(child_id);
                    let _ = child.wait();
                    process_group.disarm();
                    break None;
                }
                Err(error) => {
                    kill_process_group(child_id);
                    let _ = child.wait();
                    process_group.disarm();
                    return Err(format!("Bubblewrap version check failed: {error}"));
                }
            }
        };
        let stdout = stdout_reader
            .join()
            .map_err(|_| "Bubblewrap version stdout reader panicked".to_string())?
            .map_err(|error| format!("Bubblewrap version stdout read failed: {error}"))?;
        let stderr = stderr_reader
            .join()
            .map_err(|_| "Bubblewrap version stderr reader panicked".to_string())?
            .map_err(|error| format!("Bubblewrap version stderr read failed: {error}"))?;
        let status = status.ok_or_else(|| "Bubblewrap version check timed out".to_string())?;
        if !status.success() || stdout.overflow || stderr.overflow || !stderr.bytes.is_empty() {
            return Err(format!(
                "trusted Bubblewrap version check failed (status {status})"
            ));
        }
        let version = parse_bubblewrap_version(&stdout.bytes)?;
        if version < MINIMUM_BUBBLEWRAP_VERSION {
            return Err(format!(
                "Bubblewrap {version:?} is below the required 0.12.0 security floor"
            ));
        }
        Ok(version)
    }

    fn parse_bubblewrap_version(output: &[u8]) -> Result<(u32, u32, u32), String> {
        let output = std::str::from_utf8(output)
            .map_err(|_| "Bubblewrap version output is not UTF-8".to_string())?;
        let mut fields = output.split_whitespace();
        if fields.next() != Some("bubblewrap") {
            return Err("Bubblewrap version output has an unexpected identity".into());
        }
        let raw = fields
            .next()
            .ok_or_else(|| "Bubblewrap version output is missing a version".to_string())?;
        let numeric = raw
            .split(|character: char| !character.is_ascii_digit() && character != '.')
            .next()
            .unwrap_or_default();
        let parts = numeric
            .split('.')
            .map(|part| part.parse::<u32>())
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| "Bubblewrap version is not numeric".to_string())?;
        if parts.len() != 3 {
            return Err("Bubblewrap version must contain major, minor, and patch numbers".into());
        }
        Ok((parts[0], parts[1], parts[2]))
    }

    struct BoundedOutput {
        bytes: Vec<u8>,
        overflow: bool,
    }

    fn read_bounded(mut reader: impl Read, limit: usize) -> io::Result<BoundedOutput> {
        let mut bytes = Vec::with_capacity(limit.min(1024));
        let mut overflow = false;
        let mut buffer = [0u8; 1024];
        loop {
            let count = reader.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            let retained = count.min(limit.saturating_sub(bytes.len()));
            bytes.extend_from_slice(&buffer[..retained]);
            overflow |= retained < count;
        }
        Ok(BoundedOutput { bytes, overflow })
    }

    struct ProbeProcessGroupGuard(Option<u32>);

    impl ProbeProcessGroupGuard {
        fn disarm(&mut self) {
            self.0 = None;
        }
    }

    impl Drop for ProbeProcessGroupGuard {
        fn drop(&mut self) {
            if let Some(pid) = self.0 {
                kill_process_group(pid);
            }
        }
    }

    fn kill_process_group(pid: u32) {
        // SAFETY: negative pid addresses only the process group created by `process_group(0)`.
        unsafe {
            libc::kill(-(pid as i32), libc::SIGKILL);
        }
    }

    fn probe(workspace_roots: Vec<PathBuf>) -> Result<(u32, u32, u32), String> {
        let mut roots = Vec::with_capacity(workspace_roots.len());
        for root in workspace_roots {
            let root = root
                .canonicalize()
                .map_err(|error| format!("cannot resolve MXC probe workspace: {error}"))?;
            if !root.is_dir() {
                return Err("MXC probe workspace is not a directory".into());
            }
            let root = root
                .to_str()
                .ok_or_else(|| "MXC probe workspace path is not valid UTF-8".to_string())?
                .to_string();
            if !roots.contains(&root) {
                roots.push(root);
            }
        }
        let working_directory = roots
            .first()
            .ok_or_else(|| "MXC probe requires a workspace root".to_string())?
            .clone();
        let executable = std::env::current_exe()
            .map_err(|error| format!("cannot resolve MXC helper executable: {error}"))?;
        let control_directories = trusted_control_directories(&executable)?;
        let canonical_roots = roots.iter().map(PathBuf::from).collect::<Vec<_>>();
        ensure_control_directories_outside_workspaces(&control_directories, &canonical_roots)?;
        let bubblewrap_version = verify_bubblewrap_version()?;
        let marker = random_start_marker()?;
        let environment = vec![
            ("PATH".into(), DEFAULT_WORKLOAD_PATH.into()),
            ("HOME".into(), "/tmp/cyrene-home".into()),
            ("TMPDIR".into(), "/tmp".into()),
        ];
        let request = bubblewrap_request(
            wrapped_command("exit 0", &marker),
            roots,
            working_directory,
            environment,
            10_000,
        );
        let mut process = v1::spawn(request, SpawnOptions { telemetry: None })
            .map_err(|error| format!("MXC Bubblewrap containment probe failed: {error}"))?;
        let mut stdout = process
            .take_stdout()
            .ok_or_else(|| "MXC containment probe did not provide stdout".to_string())?;
        let stderr = process
            .take_stderr()
            .ok_or_else(|| "MXC containment probe did not provide stderr".to_string())?;
        let stderr_reader = forward_stream(stderr, io::stderr(), Arc::new(AtomicBool::new(false)));
        if let Err(error) = consume_start_marker(&mut stdout, &marker) {
            let _ = process.kill();
            let _ = process.wait();
            drop(process);
            let _ = stderr_reader.join();
            return Err(format!(
                "MXC containment probe did not start its workload: {error}"
            ));
        }
        let mut extra_stdout = Vec::new();
        stdout
            .take(128)
            .read_to_end(&mut extra_stdout)
            .map_err(|error| format!("cannot read MXC containment probe output: {error}"))?;
        let outcome = process
            .wait()
            .map_err(|error| format!("MXC containment probe wait failed: {error}"))?;
        let warnings = process.warnings();
        drop(process);
        stderr_reader
            .join()
            .map_err(|_| "MXC containment probe stderr reader panicked".to_string())?
            .map_err(|error| format!("MXC containment probe stderr read failed: {error}"))?;
        if !extra_stdout.is_empty() {
            return Err("MXC containment probe produced unexpected workload output".into());
        }
        if !warnings.is_empty() {
            return Err(format!(
                "MXC containment probe warnings: {}",
                warnings.join("; ")
            ));
        }
        if outcome != WaitResult::Exited(0) {
            return Err(format!("MXC containment probe exited as {outcome:?}"));
        }
        Ok(bubblewrap_version)
    }

    fn forward_stream<R: Read + Send + 'static, W: Write + Send + 'static>(
        mut reader: R,
        mut writer: W,
        write_failed: Arc<AtomicBool>,
    ) -> thread::JoinHandle<io::Result<()>> {
        thread::spawn(move || {
            let mut buffer = [0u8; 16 * 1024];
            loop {
                let count = reader.read(&mut buffer)?;
                if count == 0 {
                    return Ok(());
                }
                if !write_failed.load(Ordering::SeqCst)
                    && writer.write_all(&buffer[..count]).is_err()
                {
                    write_failed.store(true, Ordering::SeqCst);
                }
            }
        })
    }

    fn write_receipt(path: &Path, receipt: &MxcTerminalReceipt) -> Result<(), String> {
        let bytes = serde_json::to_vec(receipt)
            .map_err(|error| format!("cannot encode MXC terminal receipt: {error}"))?;
        if bytes.len()
            > cyrene_computer_runtime::sandbox::mxc_protocol::MAX_MXC_TERMINAL_RECEIPT_BYTES
        {
            return Err("MXC terminal receipt exceeds its size limit".into());
        }
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
            .open(path)
            .map_err(|error| format!("cannot create MXC terminal receipt: {error}"))?;
        file.write_all(&bytes)
            .and_then(|()| file.sync_all())
            .map_err(|error| format!("cannot persist MXC terminal receipt: {error}"))
    }

    fn run(paths: Paths) -> i32 {
        let control_dir = match paths.request.parent() {
            Some(parent) if paths.receipt.parent() == Some(parent) => parent,
            _ => return STDERR_ERROR_EXIT,
        };
        let request =
            read_private_json::<MxcControlRequest>(&paths.request, MAX_MXC_CONTROL_REQUEST_BYTES);
        let mut request = match request {
            Ok(request) => request,
            Err(message) => {
                let receipt = MxcTerminalReceipt::BackendError {
                    schema_version: MXC_CONTROL_SCHEMA_VERSION,
                    message: bounded_message(message),
                };
                let _ = write_receipt(&paths.receipt, &receipt);
                return STDERR_ERROR_EXIT;
            }
        };
        if let Err(message) = validate_request(&mut request, control_dir) {
            let receipt = MxcTerminalReceipt::BackendError {
                schema_version: MXC_CONTROL_SCHEMA_VERSION,
                message: bounded_message(message),
            };
            let _ = write_receipt(&paths.receipt, &receipt);
            return STDERR_ERROR_EXIT;
        }

        if let Err(message) = verify_bubblewrap_version() {
            let receipt = MxcTerminalReceipt::BackendUnavailable {
                schema_version: MXC_CONTROL_SCHEMA_VERSION,
                message: bounded_message(message),
            };
            let _ = write_receipt(&paths.receipt, &receipt);
            return STDERR_ERROR_EXIT;
        }

        // Once parsed into memory, remove the only on-disk copy of command and
        // caller environment before the workload starts.
        if let Err(error) = fs::remove_file(&paths.request) {
            let receipt = MxcTerminalReceipt::BackendError {
                schema_version: MXC_CONTROL_SCHEMA_VERSION,
                message: bounded_message(format!("cannot remove MXC request file: {error}")),
            };
            let _ = write_receipt(&paths.receipt, &receipt);
            return STDERR_ERROR_EXIT;
        }

        let started = Instant::now();
        let container_request = bubblewrap_request(
            wrapped_command(&request.command, &request.start_marker),
            request.workspace_roots.clone(),
            request.working_directory.clone(),
            request.environment.into_iter().collect(),
            request.timeout_ms,
        );

        let mut process = match v1::spawn(container_request, SpawnOptions { telemetry: None }) {
            Ok(process) => process,
            Err(error) => {
                let receipt = MxcTerminalReceipt::BackendUnavailable {
                    schema_version: MXC_CONTROL_SCHEMA_VERSION,
                    message: bounded_message(format!("MXC Bubblewrap launch failed: {error}")),
                };
                let _ = write_receipt(&paths.receipt, &receipt);
                return STDERR_ERROR_EXIT;
            }
        };
        let mut stdout = match process.take_stdout() {
            Some(stream) => stream,
            None => {
                let _ = process.kill();
                let _ = process.wait();
                let receipt = MxcTerminalReceipt::BackendUnavailable {
                    schema_version: MXC_CONTROL_SCHEMA_VERSION,
                    message: "MXC Bubblewrap did not provide stdout".into(),
                };
                let _ = write_receipt(&paths.receipt, &receipt);
                return STDERR_ERROR_EXIT;
            }
        };
        let stderr = match process.take_stderr() {
            Some(stream) => stream,
            None => {
                let _ = process.kill();
                let _ = process.wait();
                let receipt = MxcTerminalReceipt::BackendUnavailable {
                    schema_version: MXC_CONTROL_SCHEMA_VERSION,
                    message: "MXC Bubblewrap did not provide stderr".into(),
                };
                let _ = write_receipt(&paths.receipt, &receipt);
                return STDERR_ERROR_EXIT;
            }
        };
        let write_failed = Arc::new(AtomicBool::new(false));
        let stderr_reader = forward_stream(stderr, io::stderr(), Arc::clone(&write_failed));
        if let Err(error) = consume_start_marker(&mut stdout, &request.start_marker) {
            let cleanup = process.kill().and_then(|()| process.wait().map(|_| ()));
            drop(stdout);
            drop(process);
            let stderr_result = stderr_reader.join();
            let cleanup_message = cleanup.err().map(|error| error.to_string());
            let stderr_message = stderr_result
                .ok()
                .and_then(Result::err)
                .map(|error| error.to_string());
            let receipt = MxcTerminalReceipt::BackendUnavailable {
                schema_version: MXC_CONTROL_SCHEMA_VERSION,
                message: bounded_message(format!(
                    "MXC workload did not start: {error}{}{}",
                    cleanup_message
                        .map(|message| format!("; cleanup failed: {message}"))
                        .unwrap_or_default(),
                    stderr_message
                        .map(|message| format!("; stderr forwarding failed: {message}"))
                        .unwrap_or_default(),
                )),
            };
            let _ = write_receipt(&paths.receipt, &receipt);
            return STDERR_ERROR_EXIT;
        }
        let stdout_reader = forward_stream(stdout, io::stdout(), Arc::clone(&write_failed));

        let workload_deadline = started + Duration::from_millis(u64::from(request.timeout_ms));
        let wait_result = loop {
            if write_failed.load(Ordering::SeqCst) {
                if let Err(error) = process.kill() {
                    break Err(io::Error::other(format!(
                        "workload output forwarding failed and MXC kill failed: {error}"
                    )));
                }
            }
            if Instant::now() >= workload_deadline {
                let kill_result = process.kill();
                let reap_result = process.wait();
                match (kill_result, reap_result) {
                    (Ok(()), Ok(_)) | (Err(_), Ok(_)) => break Ok(WaitResult::TimedOut),
                    (_, Err(error)) => {
                        break Err(io::Error::other(format!(
                            "MXC helper deadline expired and workload reap failed: {error}"
                        )))
                    }
                }
            }
            match process.try_wait() {
                Ok(Some(_)) => break process.wait(),
                Ok(None) => thread::sleep(Duration::from_millis(20)),
                Err(error) if error.kind() == io::ErrorKind::TimedOut => {
                    break process.wait();
                }
                Err(error) => {
                    let _ = process.kill();
                    let _ = process.wait();
                    break Err(error);
                }
            }
        };
        let warnings = bounded_warnings(process.warnings());
        drop(process);
        let stdout_result = stdout_reader
            .join()
            .unwrap_or_else(|_| Err(io::Error::other("MXC stdout forwarding thread panicked")));
        let stderr_result = stderr_reader
            .join()
            .unwrap_or_else(|_| Err(io::Error::other("MXC stderr forwarding thread panicked")));
        let duration_ms = started.elapsed().as_millis().min(u64::MAX as u128) as u64;

        let receipt = match (wait_result, stdout_result, stderr_result) {
            (Ok(WaitResult::Exited(exit_code)), Ok(()), Ok(()))
                if !write_failed.load(Ordering::SeqCst) =>
            {
                MxcTerminalReceipt::Exited {
                    schema_version: MXC_CONTROL_SCHEMA_VERSION,
                    exit_code,
                    duration_ms,
                    warnings,
                }
            }
            (Ok(WaitResult::TimedOut), _, _) => MxcTerminalReceipt::TimedOut {
                schema_version: MXC_CONTROL_SCHEMA_VERSION,
                duration_ms,
                message: "MXC reported that the workload timed out".into(),
            },
            (Err(error), _, _) => MxcTerminalReceipt::BackendError {
                schema_version: MXC_CONTROL_SCHEMA_VERSION,
                message: bounded_message(format!("MXC workload wait failed: {error}")),
            },
            (_, Err(error), _) | (_, _, Err(error)) => MxcTerminalReceipt::BackendError {
                schema_version: MXC_CONTROL_SCHEMA_VERSION,
                message: bounded_message(format!("MXC workload output forwarding failed: {error}")),
            },
            _ => MxcTerminalReceipt::BackendError {
                schema_version: MXC_CONTROL_SCHEMA_VERSION,
                message: "MXC workload output forwarding failed".into(),
            },
        };
        let success = matches!(receipt, MxcTerminalReceipt::Exited { .. });
        match write_receipt(&paths.receipt, &receipt) {
            Ok(()) if success => 0,
            Ok(()) => STDERR_ERROR_EXIT,
            Err(error) => {
                eprintln!("{error}");
                STDERR_ERROR_EXIT
            }
        }
    }

    pub fn main() -> i32 {
        if let Err(message) = set_parent_death_signal() {
            eprintln!("{}", bounded_message(message));
            return STDERR_ERROR_EXIT;
        }
        let control_path = match fixed_control_path() {
            Ok(path) => path,
            Err(message) => {
                eprintln!("{}", bounded_message(message));
                return STDERR_ERROR_EXIT;
            }
        };
        std::env::set_var("PATH", control_path);
        match parse_mode(std::env::args().skip(1)) {
            Ok(HelperMode::Execute(paths)) => run(paths),
            Ok(HelperMode::Probe(roots)) => match probe(roots) {
                Ok((major, minor, patch)) => {
                    println!(
                        "CYRENE_MXC_PROBE_OK backend=mxc sdk=1.0.1 containment=bubblewrap bwrap={major}.{minor}.{patch}"
                    );
                    0
                }
                Err(message) => {
                    eprintln!("{}", bounded_message(message));
                    STDERR_ERROR_EXIT
                }
            },
            Err(message) => {
                eprintln!("{message}");
                STDERR_ERROR_EXIT
            }
        }
    }

    fn set_parent_death_signal() -> Result<(), String> {
        // Capture before installing the signal, then recheck to close the race
        // where the service parent exits during helper startup.
        let expected_parent = unsafe { libc::getppid() };
        if expected_parent <= 1 {
            return Err("MXC helper has no live service parent".into());
        }
        // SAFETY: PR_SET_PDEATHSIG accepts a signal number and no pointers.
        let result = unsafe {
            libc::prctl(
                libc::PR_SET_PDEATHSIG,
                libc::SIGKILL,
                0usize,
                0usize,
                0usize,
            )
        };
        if result != 0 {
            return Err(format!(
                "cannot set MXC helper parent-death signal: {}",
                io::Error::last_os_error()
            ));
        }
        if unsafe { libc::getppid() } != expected_parent {
            return Err("MXC service parent exited during helper startup".into());
        }
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::{
            consume_start_marker, parse_bubblewrap_version, valid_start_marker, wrapped_command,
            MINIMUM_BUBBLEWRAP_VERSION,
        };
        use std::io::Cursor;

        #[test]
        fn missing_or_mismatched_start_marker_is_rejected() {
            let marker = "CYRENE_MXC_STARTED_0123456789abcdef0123456789abcdef";
            assert!(consume_start_marker(&mut Cursor::new(Vec::<u8>::new()), marker).is_err());
            assert!(consume_start_marker(&mut Cursor::new(b"wrong\n"), marker).is_err());
            assert!(valid_start_marker(marker));
            assert!(!valid_start_marker("CYRENE_MXC_STARTED_short"));
        }

        #[test]
        fn wrapper_prints_marker_before_starting_the_user_bash_command() {
            let wrapped = wrapped_command("printf 'user command'", "CYRENE_MXC_STARTED_nonce");
            assert!(wrapped.starts_with("mkdir -p -- /tmp/cyrene-home && printf"));
            assert!(wrapped.contains("&& exec /bin/bash -c"));
            assert!(wrapped.ends_with("'printf '\\''user command'\\'''"));
        }

        #[test]
        fn bubblewrap_security_floor_rejects_unpatched_versions() {
            let old = parse_bubblewrap_version(b"bubblewrap 0.11.9\n").unwrap();
            let fixed = parse_bubblewrap_version(b"bubblewrap 0.13.1\n").unwrap();
            assert!(old < MINIMUM_BUBBLEWRAP_VERSION);
            assert!(fixed >= MINIMUM_BUBBLEWRAP_VERSION);
        }
    }
}

#[cfg(target_os = "linux")]
fn main() {
    std::process::exit(linux::main());
}

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("cyrene-mxc-exec supports Linux Bubblewrap only");
    std::process::exit(70);
}
