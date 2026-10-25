// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 output_limiter.rs                                               │
// │  Package: cyrene-computer-runtime::security                         │
// │  Role: Output limit and output bomb protection (T75, T76, T80).     │
// │                                                                     │
// │  模块职责：严格限制输出字节数，防止输出炸弹造成内存耗尽                 │
// └─────────────────────────────────────────────────────────────────────┘

pub const DEFAULT_MAX_OUTPUT_BYTES: usize = 64 * 1024; // 64 KiB | 中文：每个输出流默认上限为 64 KiB，兼容工具传输封套
pub const ABSOLUTE_MAX_OUTPUT_BYTES: usize = DEFAULT_MAX_OUTPUT_BYTES; // Tool envelope stays below the 1.5 MiB call limit | 工具封套控制在 1.5 MiB 调用限制内

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
    /// 追加数据块；达到上限后截断。
    pub fn append(&mut self, chunk: &[u8]) -> usize {
        self.total_bytes_seen += chunk.len();

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

    pub fn into_string_lossy(self) -> (String, bool) {
        let text = String::from_utf8_lossy(&self.buffer).to_string();
        (text, self.is_truncated)
    }
}
