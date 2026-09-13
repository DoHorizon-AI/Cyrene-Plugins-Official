// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 cancel.rs                                                       │
// │  Package: cyrene-agent-runtime::engine                              │
// │  Role: Thread-safe cooperative cancellation token (T51, T59).       │
// │                                                                     │
// │  模块职责：协作式原子取消令牌与超时时间管理                          │
// └─────────────────────────────────────────────────────────────────────┘

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

#[derive(Clone, Debug)]
pub struct CancellationToken {
    cancelled: Arc<AtomicBool>,
    deadline: Option<Instant>,
}

impl CancellationToken {
    pub fn new() -> Self {
        Self {
            cancelled: Arc::new(AtomicBool::new(false)),
            deadline: None,
        }
    }

    pub fn with_timeout(timeout: Duration) -> Self {
        Self {
            cancelled: Arc::new(AtomicBool::new(false)),
            deadline: Some(Instant::now() + timeout),
        }
    }

    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
    }

    pub fn is_cancelled(&self) -> bool {
        if self.cancelled.load(Ordering::SeqCst) {
            return true;
        }
        if let Some(deadline) = self.deadline {
            if Instant::now() >= deadline {
                return true;
            }
        }
        false
    }

    pub fn is_deadline_expired(&self) -> bool {
        if let Some(deadline) = self.deadline {
            Instant::now() >= deadline
        } else {
            false
        }
    }
}

impl Default for CancellationToken {
    fn default() -> Self {
        Self::new()
    }
}
