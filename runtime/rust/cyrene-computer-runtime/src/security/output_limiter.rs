// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 output_limiter.rs                                               │
// │  Package: cyrene-computer-runtime::security                         │
// │  Role: Output bounds and complete raw-stream evidence (T75-T80).   │
// │                                                                     │
// │  模块职责：限制呈现字节数，并为完整原始输出生成有界证据摘要             │
// └─────────────────────────────────────────────────────────────────────┘

use sha2::{Digest, Sha256};

pub const DEFAULT_MAX_OUTPUT_BYTES: usize = 64 * 1024; // 64 KiB | 中文：每个输出流默认上限为 64 KiB，兼容工具传输封套
pub const ABSOLUTE_MAX_OUTPUT_BYTES: usize = DEFAULT_MAX_OUTPUT_BYTES; // Tool envelope stays below the 1.5 MiB call limit | 工具封套控制在 1.5 MiB 调用限制内

pub struct OutputCollector {
    max_bytes: usize,
    buffer: Vec<u8>,
    is_truncated: bool,
    total_bytes_seen: usize,
    raw_hasher: Sha256,
}

/// Bounded text plus a digest over every raw byte observed from one stream.
pub(crate) struct CollectedOutput {
    pub(crate) text: String,
    pub(crate) truncated: bool,
    pub(crate) byte_length: u64,
    pub(crate) raw_sha256: [u8; 32],
}

impl OutputCollector {
    pub fn new(requested_max_bytes: Option<i64>) -> Self {
        let max_bytes = match requested_max_bytes {
            Some(b) if b > 0 => (b as usize).min(ABSOLUTE_MAX_OUTPUT_BYTES),
            _ => DEFAULT_MAX_OUTPUT_BYTES,
        };

        Self {
            max_bytes,
            buffer: Vec::with_capacity(4096),
            is_truncated: false,
            total_bytes_seen: 0,
            raw_hasher: Sha256::new(),
        }
    }

    /// Appends data chunk; truncates once limit is reached.
    /// 追加数据块；达到上限后截断。
    pub fn append(&mut self, chunk: &[u8]) -> usize {
        self.total_bytes_seen = self.total_bytes_seen.saturating_add(chunk.len());
        self.raw_hasher.update(chunk);

        if self.buffer.len() < self.max_bytes {
            let remaining = self.max_bytes - self.buffer.len();
            if chunk.len() <= remaining {
                self.buffer.extend_from_slice(chunk);
                chunk.len()
            } else {
                self.buffer.extend_from_slice(&chunk[..remaining]);
                self.is_truncated = true;
                remaining
            }
        } else {
            self.is_truncated = true;
            0
        }
    }

    pub fn is_truncated(&self) -> bool {
        self.is_truncated
    }

    pub fn total_bytes_seen(&self) -> usize {
        self.total_bytes_seen
    }

    pub(crate) fn into_output(self) -> CollectedOutput {
        let decoded = String::from_utf8_lossy(&self.buffer);
        let mut end = decoded.len().min(self.max_bytes);
        while !decoded.is_char_boundary(end) {
            end -= 1;
        }
        let display_truncated = end < decoded.len();
        CollectedOutput {
            text: decoded[..end].to_string(),
            truncated: self.is_truncated || display_truncated,
            byte_length: self.total_bytes_seen.min(u64::MAX as usize) as u64,
            raw_sha256: self.raw_hasher.finalize().into(),
        }
    }

    /// Converts the retained output to bounded lossy UTF-8 text.
    ///
    /// The displayed byte limit is applied after replacement conversion so
    /// malformed input cannot expand beyond the configured output allowance.
    pub fn into_string_lossy(self) -> (String, bool) {
        let output = self.into_output();
        (output.text, output.truncated)
    }
}

/// Hashes both complete raw output streams without retaining their contents.
///
/// The domain-separated encoding is stable: domain tag, stdout byte length and
/// digest, then stderr byte length and digest. Display truncation never changes
/// this value.
pub(crate) fn combined_output_hash(stdout: &CollectedOutput, stderr: &CollectedOutput) -> String {
    let mut hasher = Sha256::new();
    hasher.update(b"cyrene.computer.output.v1\0");
    hasher.update(stdout.byte_length.to_be_bytes());
    hasher.update(stdout.raw_sha256);
    hasher.update(stderr.byte_length.to_be_bytes());
    hasher.update(stderr.raw_sha256);
    format!("{:x}", hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::{combined_output_hash, OutputCollector};
    use sha2::{Digest, Sha256};

    #[test]
    fn hash_covers_raw_tail_bytes_beyond_the_display_limit() {
        let mut first_stdout = OutputCollector::new(Some(8));
        first_stdout.append(b"12345678-a");
        let mut second_stdout = OutputCollector::new(Some(8));
        second_stdout.append(b"12345678-b");
        let empty = || OutputCollector::new(Some(8)).into_output();
        let first = first_stdout.into_output();
        let second = second_stdout.into_output();
        assert_eq!(first.text, second.text);
        assert!(first.truncated && second.truncated);
        assert_ne!(
            combined_output_hash(&first, &empty()),
            combined_output_hash(&second, &empty())
        );
    }

    #[test]
    fn hashes_invalid_utf8_as_raw_bytes_and_caps_lossy_display() {
        let invalid_a = vec![0xff; 64 * 1024];
        let mut invalid_b = invalid_a.clone();
        let last = invalid_b.len() - 1;
        invalid_b[last] = 0xfe;

        let mut first = OutputCollector::new(Some(64 * 1024));
        first.append(&invalid_a);
        let first = first.into_output();
        let mut second = OutputCollector::new(Some(64 * 1024));
        second.append(&invalid_b);
        let second = second.into_output();

        assert!(first.text.len() <= 64 * 1024);
        assert!(first.truncated);
        assert_ne!(first.raw_sha256, second.raw_sha256);
        let mut expected = Sha256::new();
        expected.update(invalid_a.as_slice());
        assert_eq!(first.raw_sha256, <[u8; 32]>::from(expected.finalize()));
    }
}
