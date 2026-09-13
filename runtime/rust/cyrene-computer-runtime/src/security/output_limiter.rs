// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 output_limiter.rs                                               │
// │  Package: cyrene-computer-runtime::security                         │
// │  Role: Output limit and output bomb protection (T75, T76, T80).     │
// │                                                                     │
// │  模块职责：严格限制输出字节数，防止输出炸弹造成内存耗尽                 │
// └─────────────────────────────────────────────────────────────────────┘

pub const DEFAULT_MAX_OUTPUT_BYTES: usize = 512 * 1024; // 512 KB
pub const ABSOLUTE_MAX_OUTPUT_BYTES: usize = 4 * 1024 * 1024; // 4 MB

pub struct OutputCollector {
    max_bytes: usize,
    buffer: Vec<u8>,
    is_truncated: bool,
    total_bytes_seen: usize,
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
        }
    }

    /// Appends data chunk; truncates once limit is reached.
    pub fn append(&mut self, chunk: &[u8]) {
        self.total_bytes_seen += chunk.len();

        if self.buffer.len() < self.max_bytes {
            let remaining = self.max_bytes - self.buffer.len();
            if chunk.len() <= remaining {
                self.buffer.extend_from_slice(chunk);
            } else {
                self.buffer.extend_from_slice(&chunk[..remaining]);
                self.is_truncated = true;
            }
        } else {
            self.is_truncated = true;
        }
    }

    pub fn is_truncated(&self) -> bool {
        self.is_truncated
    }

    pub fn total_bytes_seen(&self) -> usize {
        self.total_bytes_seen
    }

    pub fn into_string_lossy(self) -> (String, bool) {
        let text = String::from_utf8_lossy(&self.buffer).to_string();
        (text, self.is_truncated)
    }
}
