#![allow(clippy::not_unsafe_ptr_arg_deref)]
#![allow(clippy::missing_safety_doc)]
//! ┌─────────────────────────────────────────────────────────────────────────┐
//! │ 📄 lib.rs                                                               │
//! │ Module: runtime::rust::cyrene-plugin-cabi                               │
//! │ Role: Cyrene native C ABI v1 cdylib implementation in Rust.            │
//! │                                                                         │
//! │ 模块职责：Cyrene 原生 C ABI v1 共享库 (cdylib) Rust 实现                 │
//! │ · 导出 cyrene_plugin_*_v1 7 个标准符号                                    │
//! │ · 全入口点通过 catch_unwind 封装，杜绝任何 panic 穿越 ABI 边界             │
//! │ · 完整跟踪分配缓冲区，防范重复释放 (double free) 与非法指针释放            │
//! │ · 严格遵循 Allocator Ownership 与固定宽度整数规范                        │
//! └─────────────────────────────────────────────────────────────────────────┘

use std::alloc::{alloc, dealloc, Layout};
use std::collections::{HashMap, HashSet};
use std::ffi::{CStr, CString};
use std::os::raw::c_char;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

// ── ABI Constants & Error Codes ──────────────────────────────────────────────
// ── ABI 常量与错误码 ──────────────────────────────────────────────
pub const CYRENE_ABI_VERSION_1: u32 = 1;
pub const CYRENE_MAX_PAYLOAD_SIZE: u64 = 64 * 1024 * 1024; // 64 MB | 中文：负载大小上限为 64 MB
const ALLOC_MAGIC: u64 = 0x435952454E453031; // "CYRENE01" in ASCII hex；即 "CYRENE01" 的 ASCII 十六进制表示

#[repr(i32)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatusCode {
    Ok = 0,
    Cancelled = 1,
    Unknown = 2,
    InvalidArgument = 3,
    DeadlineExceeded = 4,
    NotFound = 5,
    AlreadyExists = 6,
    PermissionDenied = 7,
    ResourceExhausted = 8,
    FailedPrecondition = 9,
    Aborted = 10,
    OutOfRange = 11,
    Unimplemented = 12,
    Internal = 13,
    Unavailable = 14,
    DataLoss = 15,
    Unauthenticated = 16,
}

// ── C ABI Struct Definitions Matching cyrene_plugin_abi_v1.h ─────────────────
// ── 与 cyrene_plugin_abi_v1.h 对应的 C ABI 结构体定义 ─────────────────
#[repr(C)]
pub struct CapabilityDescriptorV1 {
    pub capability_id: *const c_char,
    pub version_major: u32,
    pub version_minor: u32,
}
unsafe impl Sync for CapabilityDescriptorV1 {}
unsafe impl Send for CapabilityDescriptorV1 {}

#[repr(C)]
pub struct ApiInfoV1 {
    pub abi_version: u32,
    pub interface_version_major: u32,
    pub interface_version_minor: u32,
    pub implementation_name: *const c_char,
    pub implementation_version: *const c_char,
    pub capability_count: u32,
    pub capabilities: *const CapabilityDescriptorV1,
}

#[repr(C)]
pub struct CreateOptionsV1 {
    pub config_json: *const c_char,
    pub max_concurrency: u64,
    pub memory_limit_bytes: u64,
}

#[repr(C)]
pub struct InvokeRequestV1 {
    pub method: *const c_char,
    pub input_type_url: *const c_char,
    pub input_data: *const u8,
    pub input_size: u64,
    pub deadline_ms: u64,
}

#[repr(C)]
pub struct InvokeResponseV1 {
    pub status_code: i32,
    pub error_message: *mut c_char,
    pub output_type_url: *mut c_char,
    pub output_data: *mut u8,
    pub output_size: u64,
}

#[repr(C)]
pub struct StreamEventV1 {
    pub sequence_number: u64,
    pub event_type: i32,
    pub status_code: i32,
    pub error_message: *const c_char,
    pub type_url: *const c_char,
    pub payload_data: *const u8,
    pub payload_size: u64,
    pub is_terminal: bool,
}

pub type StreamCallbackV1 =
    unsafe extern "C" fn(event: *const StreamEventV1, user_data: *mut std::ffi::c_void);

#[repr(C)]
pub struct StreamRequestV1 {
    pub method: *const c_char,
    pub input_type_url: *const c_char,
    pub input_data: *const u8,
    pub input_size: u64,
    pub deadline_ms: u64,
}

// ── Tracked Allocator Header ────────────────────────────────────────────────
// ── 受跟踪分配器的头部 ────────────────────────────────────────────────
#[repr(C)]
struct AllocHeader {
    magic: u64,
    size: usize,
    align: usize,
}

// ── Static Metadata ──────────────────────────────────────────────────────────
// ── 静态元数据 ──────────────────────────────────────────────────────────
static IMPLEMENTATION_NAME: &[u8] = b"cyrene-plugin-rust\0";
static IMPLEMENTATION_VERSION: &[u8] = b"0.1.0\0";

static CAP_AGENT: &[u8] = b"agent.runtime.v1\0";
static CAP_COMPUTER: &[u8] = b"computer.runtime.v1\0";
static CAP_MEMORY: &[u8] = b"memory.provider.v1\0";
static CAP_MODEL: &[u8] = b"model.provider.v1\0";

static CAPABILITIES: [CapabilityDescriptorV1; 4] = [
    CapabilityDescriptorV1 {
        capability_id: CAP_AGENT.as_ptr() as *const c_char,
        version_major: 1,
        version_minor: 0,
    },
    CapabilityDescriptorV1 {
        capability_id: CAP_COMPUTER.as_ptr() as *const c_char,
        version_major: 1,
        version_minor: 0,
    },
    CapabilityDescriptorV1 {
        capability_id: CAP_MEMORY.as_ptr() as *const c_char,
        version_major: 1,
        version_minor: 0,
    },
    CapabilityDescriptorV1 {
        capability_id: CAP_MODEL.as_ptr() as *const c_char,
        version_major: 1,
        version_minor: 0,
    },
];

// ── Plugin Instance Internals ────────────────────────────────────────────────
// ── 插件实例内部状态 ────────────────────────────────────────────────
pub struct PluginInstance {
    #[allow(dead_code)]
    config: Option<String>,
    #[allow(dead_code)]
    max_concurrency: u64,
    #[allow(dead_code)]
    memory_limit_bytes: u64,
    allocations: Mutex<HashSet<usize>>,
    cancel_tokens: Mutex<HashMap<usize, Arc<AtomicBool>>>,
    next_cancel_id: AtomicUsize,
    is_destroyed: AtomicBool,
}

impl PluginInstance {
    pub fn new(options: Option<&CreateOptionsV1>) -> Result<Self, StatusCode> {
        let (config, max_concurrency, memory_limit_bytes) = if let Some(opts) = options {
            let cfg = if !opts.config_json.is_null() {
                let cstr = unsafe { CStr::from_ptr(opts.config_json) };
                Some(
                    cstr.to_str()
                        .map_err(|_| StatusCode::InvalidArgument)?
                        .to_string(),
                )
            } else {
                None
            };
            (cfg, opts.max_concurrency, opts.memory_limit_bytes)
        } else {
            (None, 0, 0)
        };

        Ok(Self {
            config,
            max_concurrency,
            memory_limit_bytes,
            allocations: Mutex::new(HashSet::new()),
            cancel_tokens: Mutex::new(HashMap::new()),
            next_cancel_id: AtomicUsize::new(1),
            is_destroyed: AtomicBool::new(false),
        })
    }

    /// Allocates a buffer tracked by this instance's allocation ledger.
    /// 分配缓冲区，并将其登记到此实例的分配账本中。
    pub fn allocate_tracked_buffer(&self, data: &[u8]) -> Result<*mut u8, StatusCode> {
        let header_size = std::mem::size_of::<AllocHeader>();
        let total_size = header_size + data.len();
        let align = std::mem::align_of::<AllocHeader>();
        let layout =
            Layout::from_size_align(total_size, align).map_err(|_| StatusCode::Internal)?;

        unsafe {
            let raw_mem = alloc(layout);
            if raw_mem.is_null() {
                return Err(StatusCode::ResourceExhausted);
            }

            let header_ptr = raw_mem as *mut AllocHeader;
            header_ptr.write(AllocHeader {
                magic: ALLOC_MAGIC,
                size: data.len(),
                align,
            });

            let user_ptr = raw_mem.add(header_size);
            std::ptr::copy_nonoverlapping(data.as_ptr(), user_ptr, data.len());

            let mut allocs = self.allocations.lock().unwrap();
            allocs.insert(user_ptr as usize);

            Ok(user_ptr)
        }
    }

    /// Allocates a null-terminated C string buffer tracked by this instance.
    /// 分配以空字符结尾的 C 字符串缓冲区，并由此实例跟踪。
    pub fn allocate_tracked_string(&self, s: &str) -> Result<*mut c_char, StatusCode> {
        let mut bytes = s.as_bytes().to_vec();
        bytes.push(0); // Null terminator；空字符终止符
        let ptr = self.allocate_tracked_buffer(&bytes)?;
        Ok(ptr as *mut c_char)
    }

    /// Frees a buffer previously allocated and tracked by this instance.
    /// 释放此前由此实例分配并跟踪的缓冲区。
    pub unsafe fn free_buffer(&self, ptr: *mut u8) -> StatusCode {
        if ptr.is_null() {
            return StatusCode::Ok;
        }

        let user_addr = ptr as usize;
        let mut allocs = self.allocations.lock().unwrap();
        if !allocs.remove(&user_addr) {
            // Buffer was not allocated by this instance or has already been freed (double free attempt)
            return StatusCode::InvalidArgument;
        }

        let header_size = std::mem::size_of::<AllocHeader>();
        unsafe {
            let raw_mem = ptr.sub(header_size);
            let header_ptr = raw_mem as *mut AllocHeader;
            if (*header_ptr).magic != ALLOC_MAGIC {
                return StatusCode::InvalidArgument;
            }

            let total_size = header_size + (*header_ptr).size;
            let align = (*header_ptr).align;
            (*header_ptr).magic = 0; // Invalidate header；将头部标记为无效

            let layout = match Layout::from_size_align(total_size, align) {
                Ok(l) => l,
                Err(_) => return StatusCode::Internal,
            };

            dealloc(raw_mem, layout);
        }

        StatusCode::Ok
    }

    /// Registers a new cancellation token and returns its handle identifier.
    /// 注册新的取消令牌并返回其句柄标识。
    pub fn register_cancel_token(&self) -> (usize, Arc<AtomicBool>) {
        let token_id = self.next_cancel_id.fetch_add(1, Ordering::SeqCst);
        let flag = Arc::new(AtomicBool::new(false));
        let mut tokens = self.cancel_tokens.lock().unwrap();
        tokens.insert(token_id, Arc::clone(&flag));
        (token_id, flag)
    }

    /// Removes a cancellation token after stream completion.
    /// 流完成后移除对应的取消令牌。
    pub fn unregister_cancel_token(&self, token_id: usize) {
        let mut tokens = self.cancel_tokens.lock().unwrap();
        tokens.remove(&token_id);
    }

    /// Triggers cancellation for a registered token handle.
    /// 触发指定已注册令牌句柄的取消操作。
    pub fn trigger_cancellation(&self, token_id: usize) -> StatusCode {
        let tokens = self.cancel_tokens.lock().unwrap();
        if let Some(flag) = tokens.get(&token_id) {
            flag.store(true, Ordering::SeqCst);
            StatusCode::Ok
        } else {
            // Already finished or cancelled; safe idempotent response
            // 已经结束或取消；可安全地幂等返回
            StatusCode::Ok
        }
    }
}

impl Drop for PluginInstance {
    fn drop(&mut self) {
        self.is_destroyed.store(true, Ordering::SeqCst);
        let mut allocs = self.allocations.lock().unwrap();
        let header_size = std::mem::size_of::<AllocHeader>();

        for &addr in allocs.iter() {
            unsafe {
                let user_ptr = addr as *mut u8;
                let raw_mem = user_ptr.sub(header_size);
                let header_ptr = raw_mem as *mut AllocHeader;
                if (*header_ptr).magic == ALLOC_MAGIC {
                    let total_size = header_size + (*header_ptr).size;
                    let align = (*header_ptr).align;
                    (*header_ptr).magic = 0;
                    if let Ok(layout) = Layout::from_size_align(total_size, align) {
                        dealloc(raw_mem, layout);
                    }
                }
            }
        }
        allocs.clear();
    }
}

// ── Helper Functions ─────────────────────────────────────────────────────────
// ── 辅助函数 ─────────────────────────────────────────────────────────
fn current_epoch_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

// ═════════════════════════════════════════════════════════════════════════════
//                        EXPORTED C ABI V1 ENTRYPOINTS
// ═════════════════════════════════════════════════════════════════════════════
// ═════════════════════════════════════════════════════════════════════════════
// 导出的 C ABI V1 入口点
// ═════════════════════════════════════════════════════════════════════════════

/// ════════════════════════════════════════════════════════════════════════════
/// @brief Negotiates ABI, capability list, and interface version.
/// ════════════════════════════════════════════════════════════════════════════
/// ════════════════════════════════════════════════════════════════════════════
/// @brief 协商 ABI、能力列表和接口版本。
/// ════════════════════════════════════════════════════════════════════════════
#[no_mangle]
pub unsafe extern "C" fn cyrene_plugin_get_api_v1(
    requested_abi_version: u32,
    out_info: *mut ApiInfoV1,
) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        if out_info.is_null() {
            return StatusCode::InvalidArgument;
        }

        if requested_abi_version != CYRENE_ABI_VERSION_1 {
            (*out_info).abi_version = CYRENE_ABI_VERSION_1;
            return StatusCode::FailedPrecondition;
        }

        (*out_info).abi_version = CYRENE_ABI_VERSION_1;
        (*out_info).interface_version_major = 1;
        (*out_info).interface_version_minor = 0;
        (*out_info).implementation_name = IMPLEMENTATION_NAME.as_ptr() as *const c_char;
        (*out_info).implementation_version = IMPLEMENTATION_VERSION.as_ptr() as *const c_char;
        (*out_info).capability_count = CAPABILITIES.len() as u32;
        (*out_info).capabilities = CAPABILITIES.as_ptr();

        StatusCode::Ok
    }));

    match result {
        Ok(status) => status as i32,
        Err(_) => StatusCode::Internal as i32,
    }
}

/// ════════════════════════════════════════════════════════════════════════════
/// @brief Creates and initializes a new plugin instance.
/// ════════════════════════════════════════════════════════════════════════════
/// ════════════════════════════════════════════════════════════════════════════
/// @brief 创建并初始化新的插件实例。
/// ════════════════════════════════════════════════════════════════════════════
#[no_mangle]
pub unsafe extern "C" fn cyrene_plugin_create_v1(
    options: *const CreateOptionsV1,
    out_handle: *mut *mut std::ffi::c_void,
) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        if out_handle.is_null() {
            return StatusCode::InvalidArgument;
        }

        let opts_ref = if !options.is_null() {
            Some(&*options)
        } else {
            None
        };
        let instance = match PluginInstance::new(opts_ref) {
            Ok(inst) => inst,
            Err(e) => return e,
        };

        let boxed = Box::new(instance);
        let raw = Box::into_raw(boxed) as *mut std::ffi::c_void;
        *out_handle = raw;

        StatusCode::Ok
    }));

    match result {
        Ok(status) => status as i32,
        Err(_) => StatusCode::Internal as i32,
    }
}

/// ════════════════════════════════════════════════════════════════════════════
/// @brief Executes a synchronous blocking unary invocation.
/// ════════════════════════════════════════════════════════════════════════════
/// ════════════════════════════════════════════════════════════════════════════
/// @brief 执行同步阻塞的一元调用。
/// ════════════════════════════════════════════════════════════════════════════
#[no_mangle]
pub unsafe extern "C" fn cyrene_plugin_invoke_v1(
    handle: *mut std::ffi::c_void,
    request: *const InvokeRequestV1,
    out_response: *mut InvokeResponseV1,
) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        if handle.is_null() || request.is_null() || out_response.is_null() {
            return StatusCode::InvalidArgument;
        }

        let instance = &*(handle as *mut PluginInstance);
        if instance.is_destroyed.load(Ordering::SeqCst) {
            return StatusCode::FailedPrecondition;
        }

        let req = &*request;

        // Check input payload limits
        // 检查输入载荷大小限制
        if req.input_size > CYRENE_MAX_PAYLOAD_SIZE {
            (*out_response).status_code = StatusCode::ResourceExhausted as i32;
            (*out_response).error_message = instance
                .allocate_tracked_string("Payload size exceeds maximum allowed 64 MB limit")
                .unwrap_or(std::ptr::null_mut());
            (*out_response).output_type_url = std::ptr::null_mut();
            (*out_response).output_data = std::ptr::null_mut();
            (*out_response).output_size = 0;
            return StatusCode::ResourceExhausted;
        }

        // Validate string inputs for null and UTF-8 correctness
        // 检查字符串输入是否为空指针，并验证 UTF-8 编码
        if req.method.is_null() {
            return StatusCode::InvalidArgument;
        }
        let method = match CStr::from_ptr(req.method).to_str() {
            Ok(s) => s,
            Err(_) => return StatusCode::InvalidArgument,
        };

        let type_url = if !req.input_type_url.is_null() {
            match CStr::from_ptr(req.input_type_url).to_str() {
                Ok(s) => s,
                Err(_) => return StatusCode::InvalidArgument,
            }
        } else {
            ""
        };

        // Check deadline
        // 检查截止时间
        if req.deadline_ms > 0 && current_epoch_ms() > req.deadline_ms {
            (*out_response).status_code = StatusCode::DeadlineExceeded as i32;
            (*out_response).error_message = instance
                .allocate_tracked_string("Deadline exceeded prior to invocation")
                .unwrap_or(std::ptr::null_mut());
            (*out_response).output_type_url = std::ptr::null_mut();
            (*out_response).output_data = std::ptr::null_mut();
            (*out_response).output_size = 0;
            return StatusCode::DeadlineExceeded;
        }

        // Process request based on method
        // 根据方法处理请求
        let input_bytes = if !req.input_data.is_null() && req.input_size > 0 {
            std::slice::from_raw_parts(req.input_data, req.input_size as usize)
        } else {
            &[]
        };

        // Standard Echo & Capability Round-trip dispatcher
        // 标准 Echo 与能力往返分发器
        let (output_bytes, out_type_url) = match method {
            "Echo" | "cyrene.plugin.runtime.v1.DirectPluginRuntime/Invoke" => {
                (input_bytes.to_vec(), type_url.to_string())
            }
            "agent.runtime.v1/Run" => {
                // Return synthetic or reflected response
                // 返回合成响应或原样映射的响应
                let mut resp_data = Vec::new();
                resp_data.extend_from_slice(b"\x0a\x06run_01\x10\x00"); // Minimal protobuf fields；最小化的 Protobuf 字段
                (
                    resp_data,
                    "type.cyrene.io/cyrene.agent.runtime.v1.AgentRunResponse".to_string(),
                )
            }
            "health" => (
                b"\x08\x01".to_vec(),
                "type.cyrene.io/cyrene.plugin.runtime.v1.HealthResponse".to_string(),
            ),
            _ => (input_bytes.to_vec(), type_url.to_string()),
        };

        // Allocate outputs tracked by instance
        // 分配由实例跟踪的输出缓冲区
        let out_data_ptr = match instance.allocate_tracked_buffer(&output_bytes) {
            Ok(p) => p,
            Err(e) => return e,
        };

        let out_type_url_ptr = match instance.allocate_tracked_string(&out_type_url) {
            Ok(p) => p,
            Err(e) => {
                instance.free_buffer(out_data_ptr);
                return e;
            }
        };

        (*out_response).status_code = StatusCode::Ok as i32;
        (*out_response).error_message = std::ptr::null_mut();
        (*out_response).output_type_url = out_type_url_ptr;
        (*out_response).output_data = out_data_ptr;
        (*out_response).output_size = output_bytes.len() as u64;

        StatusCode::Ok
    }));

    match result {
        Ok(status) => status as i32,
        Err(_) => StatusCode::Internal as i32,
    }
}

/// ════════════════════════════════════════════════════════════════════════════
/// @brief Initiates a streaming invocation, delivering events through a callback.
/// ════════════════════════════════════════════════════════════════════════════
/// ════════════════════════════════════════════════════════════════════════════
/// @brief 启动流式调用，并通过回调传递事件。
/// ════════════════════════════════════════════════════════════════════════════
#[no_mangle]
pub unsafe extern "C" fn cyrene_plugin_invoke_stream_v1(
    handle: *mut std::ffi::c_void,
    request: *const StreamRequestV1,
    callback: Option<StreamCallbackV1>,
    user_data: *mut std::ffi::c_void,
    out_cancel_handle: *mut *mut std::ffi::c_void,
) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        if handle.is_null() || request.is_null() {
            return StatusCode::InvalidArgument;
        }

        let cb = match callback {
            Some(f) => f,
            None => return StatusCode::InvalidArgument,
        };

        let instance = &*(handle as *mut PluginInstance);
        if instance.is_destroyed.load(Ordering::SeqCst) {
            return StatusCode::FailedPrecondition;
        }

        let req = &*request;
        if req.input_size > CYRENE_MAX_PAYLOAD_SIZE {
            return StatusCode::ResourceExhausted;
        }

        // Register cancellation token
        // 注册取消令牌
        let (token_id, cancel_flag) = instance.register_cancel_token();
        if !out_cancel_handle.is_null() {
            *out_cancel_handle = token_id as *mut std::ffi::c_void;
        }

        let input_bytes = if !req.input_data.is_null() && req.input_size > 0 {
            std::slice::from_raw_parts(req.input_data, req.input_size as usize)
        } else {
            &[]
        };

        let type_url_cstr = if !req.input_type_url.is_null() {
            CStr::from_ptr(req.input_type_url)
        } else {
            c""
        };

        // Emit chunks (3 incremental events + 1 terminal event)
        // 发送数据块（3 个增量事件和 1 个终态事件）
        let num_chunks = 3;
        let mut cancelled = false;

        for seq in 0..num_chunks {
            // Check cancellation
            // 检查是否已取消
            if cancel_flag.load(Ordering::SeqCst) {
                cancelled = true;
                break;
            }

            // Check deadline
            // 检查截止时间
            if req.deadline_ms > 0 && current_epoch_ms() > req.deadline_ms {
                let err_msg = CString::new("Stream deadline exceeded").unwrap();
                let event = StreamEventV1 {
                    sequence_number: seq,
                    event_type: 3, // CYRENE_STREAM_EVENT_ERROR（错误事件）
                    status_code: StatusCode::DeadlineExceeded as i32,
                    error_message: err_msg.as_ptr(),
                    type_url: std::ptr::null(),
                    payload_data: std::ptr::null(),
                    payload_size: 0,
                    is_terminal: true,
                };
                cb(&event, user_data);
                instance.unregister_cancel_token(token_id);
                return StatusCode::DeadlineExceeded;
            }

            let chunk_data = if !input_bytes.is_empty() {
                input_bytes
            } else {
                b"data_chunk"
            };

            let event = StreamEventV1 {
                sequence_number: seq,
                event_type: 0, // CYRENE_STREAM_EVENT_DATA（数据事件）
                status_code: StatusCode::Ok as i32,
                error_message: std::ptr::null(),
                type_url: type_url_cstr.as_ptr(),
                payload_data: chunk_data.as_ptr(),
                payload_size: chunk_data.len() as u64,
                is_terminal: false,
            };

            cb(&event, user_data);
        }

        // Emit terminal event
        // 发送终态事件
        if cancelled {
            let cancel_msg = CString::new("Stream cancelled by client").unwrap();
            let event = StreamEventV1 {
                sequence_number: num_chunks,
                event_type: 2, // CYRENE_STREAM_EVENT_CANCELLED（取消事件）
                status_code: StatusCode::Cancelled as i32,
                error_message: cancel_msg.as_ptr(),
                type_url: std::ptr::null(),
                payload_data: std::ptr::null(),
                payload_size: 0,
                is_terminal: true,
            };
            cb(&event, user_data);
        } else {
            let event = StreamEventV1 {
                sequence_number: num_chunks,
                event_type: 1, // CYRENE_STREAM_EVENT_TERMINAL（终态事件）
                status_code: StatusCode::Ok as i32,
                error_message: std::ptr::null(),
                type_url: std::ptr::null(),
                payload_data: std::ptr::null(),
                payload_size: 0,
                is_terminal: true,
            };
            cb(&event, user_data);
        }

        instance.unregister_cancel_token(token_id);
        StatusCode::Ok
    }));

    match result {
        Ok(status) => status as i32,
        Err(_) => StatusCode::Internal as i32,
    }
}

/// ════════════════════════════════════════════════════════════════════════════
/// @brief Asynchronously requests cancellation of an ongoing stream operation.
/// ════════════════════════════════════════════════════════════════════════════
/// ════════════════════════════════════════════════════════════════════════════
/// @brief 异步请求取消正在进行的流操作。
/// ════════════════════════════════════════════════════════════════════════════
#[no_mangle]
pub unsafe extern "C" fn cyrene_plugin_cancel_v1(
    handle: *mut std::ffi::c_void,
    cancel_handle: *mut std::ffi::c_void,
) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        if handle.is_null() {
            return StatusCode::InvalidArgument;
        }

        let instance = &*(handle as *mut PluginInstance);
        if instance.is_destroyed.load(Ordering::SeqCst) {
            return StatusCode::FailedPrecondition;
        }

        let token_id = cancel_handle as usize;
        instance.trigger_cancellation(token_id)
    }));

    match result {
        Ok(status) => status as i32,
        Err(_) => StatusCode::Internal as i32,
    }
}

/// ════════════════════════════════════════════════════════════════════════════
/// @brief Releases a buffer allocated by this plugin library.
/// ════════════════════════════════════════════════════════════════════════════
/// ════════════════════════════════════════════════════════════════════════════
/// @brief 释放由此插件库分配的缓冲区。
/// ════════════════════════════════════════════════════════════════════════════
#[no_mangle]
pub unsafe extern "C" fn cyrene_plugin_free_buffer_v1(
    handle: *mut std::ffi::c_void,
    buffer: *mut std::ffi::c_void,
) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        if buffer.is_null() {
            return StatusCode::Ok;
        }
        if handle.is_null() {
            return StatusCode::InvalidArgument;
        }

        let instance = &*(handle as *mut PluginInstance);
        instance.free_buffer(buffer as *mut u8)
    }));

    match result {
        Ok(status) => status as i32,
        Err(_) => StatusCode::Internal as i32,
    }
}

/// ════════════════════════════════════════════════════════════════════════════
/// @brief Destroys a plugin instance and cleans up all allocated resources.
/// ════════════════════════════════════════════════════════════════════════════
/// ════════════════════════════════════════════════════════════════════════════
/// @brief 销毁插件实例并清理所有已分配资源。
/// ════════════════════════════════════════════════════════════════════════════
#[no_mangle]
pub unsafe extern "C" fn cyrene_plugin_destroy_v1(handle: *mut std::ffi::c_void) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        if handle.is_null() {
            return StatusCode::InvalidArgument;
        }

        let raw = handle as *mut PluginInstance;
        // Verify not double-destroyed
        // 验证实例尚未被重复销毁
        if (*raw).is_destroyed.swap(true, Ordering::SeqCst) {
            return StatusCode::InvalidArgument;
        }

        drop(Box::from_raw(raw));
        StatusCode::Ok
    }));

    match result {
        Ok(status) => status as i32,
        Err(_) => StatusCode::Internal as i32,
    }
}
