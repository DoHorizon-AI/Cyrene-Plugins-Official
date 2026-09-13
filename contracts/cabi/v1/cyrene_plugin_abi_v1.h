/* ┌─────────────────────────────────────────────────────────────────────────┐ */
/* │ 📄 cyrene_plugin_abi_v1.h                                               │ */
/* │ Module: contracts/cabi/v1                                               │ */
/* │ Role: Versioned pure C ABI surface for Cyrene native plugins.           │ */
/* │                                                                         │ */
/* │ 模块职责：Cyrene 原生插件版本化纯 C ABI 接口定义                          │ */
/* │ · 基于固定宽度整数与不透明句柄 (opaque handles)                            │ */
/* │ · 仅通过 type_url + protobuf 字节流通信，禁止裸露语言级内存布局             │ */
/* │ · 严格遵循 Allocator Ownership（谁分配谁释放）原则                          │ */
/* │ · 禁止任何 Rust panic、C++ 异常或 C# 异常穿越 ABI 边界                    │ */
/* └─────────────────────────────────────────────────────────────────────────┘ */

#ifndef CYRENE_PLUGIN_ABI_V1_H
#define CYRENE_PLUGIN_ABI_V1_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ── Symbol Export/Import Macros ─────────────────────────────────────────── */
#if defined(_WIN32) || defined(__CYGWIN__)
    #if defined(CYRENE_ABI_BUILDING_DLL)
        #define CYRENE_ABI_EXPORT __declspec(dllexport)
    #else
        #define CYRENE_ABI_EXPORT __declspec(dllimport)
    #endif
#else
    #if defined(__GNUC__) && __GNUC__ >= 4
        #define CYRENE_ABI_EXPORT __attribute__((visibility("default")))
    #else
        #define CYRENE_ABI_EXPORT
    #endif
#endif

/* ── Constants & Limits ──────────────────────────────────────────────────── */
#define CYRENE_ABI_VERSION_1 1U
#define CYRENE_MAX_PAYLOAD_SIZE (64ULL * 1024ULL * 1024ULL) /* 64 MB maximum payload */

/* ── Status Codes (gRPC / DirectPluginRuntime Aligned) ───────────────────── */
typedef enum cyrene_status_code_v1 {
    CYRENE_STATUS_OK                  = 0,
    CYRENE_STATUS_CANCELLED           = 1,
    CYRENE_STATUS_UNKNOWN             = 2,
    CYRENE_STATUS_INVALID_ARGUMENT    = 3,
    CYRENE_STATUS_DEADLINE_EXCEEDED   = 4,
    CYRENE_STATUS_NOT_FOUND           = 5,
    CYRENE_STATUS_ALREADY_EXISTS      = 6,
    CYRENE_STATUS_PERMISSION_DENIED   = 7,
    CYRENE_STATUS_RESOURCE_EXHAUSTED  = 8,
    CYRENE_STATUS_FAILED_PRECONDITION = 9,
    CYRENE_STATUS_ABORTED             = 10,
    CYRENE_STATUS_OUT_OF_RANGE        = 11,
    CYRENE_STATUS_UNIMPLEMENTED       = 12,
    CYRENE_STATUS_INTERNAL            = 13,
    CYRENE_STATUS_UNAVAILABLE         = 14,
    CYRENE_STATUS_DATA_LOSS           = 15,
    CYRENE_STATUS_UNAUTHENTICATED     = 16
} cyrene_status_code_v1_t;

/* ── Opaque Handles ──────────────────────────────────────────────────────── */
/**
 * @brief Opaque handle representing an initialized plugin instance.
 * 不透明插件实例句柄。
 */
typedef struct cyrene_plugin_instance_s* cyrene_plugin_handle_t;

/**
 * @brief Opaque handle representing an in-flight cancellable operation.
 * 不透明操作取消句柄。
 */
typedef struct cyrene_cancel_token_s* cyrene_cancel_handle_t;

/* ── Capability & API Negotiation Descriptors ────────────────────────────── */
typedef struct cyrene_capability_descriptor_v1 {
    const char* capability_id;       /* Null-terminated UTF-8, e.g. "model.provider.v1" */
    uint32_t version_major;          /* e.g. 1 */
    uint32_t version_minor;          /* e.g. 0 */
} cyrene_capability_descriptor_v1_t;

typedef struct cyrene_api_info_v1 {
    uint32_t abi_version;                                   /* Must match CYRENE_ABI_VERSION_1 */
    uint32_t interface_version_major;                      /* Core interface major */
    uint32_t interface_version_minor;                      /* Core interface minor */
    const char* implementation_name;                       /* e.g. "cyrene-plugin-rust" or "cyrene-plugin-dotnet" */
    const char* implementation_version;                    /* Semantic version string */
    uint32_t capability_count;                             /* Count of supported capabilities */
    const cyrene_capability_descriptor_v1_t* capabilities; /* Pointer to static/constant array */
} cyrene_api_info_v1_t;

/* ── Plugin Creation Options ─────────────────────────────────────────────── */
typedef struct cyrene_create_options_v1 {
    const char* config_json;         /* Optional UTF-8 null-terminated JSON configuration string, or NULL */
    uint64_t max_concurrency;        /* Concurrency bound (0 for library default) */
    uint64_t memory_limit_bytes;     /* Optional memory budget hint (0 for unlimited) */
} cyrene_create_options_v1_t;

/* ── Unary Invoke Payloads ───────────────────────────────────────────────── */
typedef struct cyrene_invoke_request_v1 {
    const char* method;              /* Method or RPC name, e.g. "Invoke" or "chat_completion" */
    const char* input_type_url;      /* Null-terminated UTF-8 type_url, e.g. "type.cyrene.io/..." */
    const uint8_t* input_data;       /* Caller-allocated Protobuf wire bytes (read-only) */
    uint64_t input_size;             /* Length of input bytes */
    uint64_t deadline_ms;            /* Wall-clock deadline in epoch milliseconds (0 = no deadline) */
} cyrene_invoke_request_v1_t;

/**
 * @brief Output structure for unary invocation.
 * 
 * ALLOCATOR CONTRACT:
 * - If status_code != CYRENE_STATUS_OK, error_message may be non-NULL.
 * - If non-NULL, error_message, output_type_url, and output_data are allocated
 *   by the plugin library.
 * - The CALLER MUST release them by passing each non-NULL buffer to
 *   cyrene_plugin_free_buffer_v1(handle, ptr).
 */
typedef struct cyrene_invoke_response_v1 {
    int32_t status_code;             /* cyrene_status_code_v1_t */
    char* error_message;             /* UTF-8 error explanation allocated by plugin; free via cyrene_plugin_free_buffer_v1 */
    char* output_type_url;           /* UTF-8 type_url allocated by plugin; free via cyrene_plugin_free_buffer_v1 */
    uint8_t* output_data;            /* Output Protobuf wire bytes allocated by plugin; free via cyrene_plugin_free_buffer_v1 */
    uint64_t output_size;            /* Length of output_data bytes */
} cyrene_invoke_response_v1_t;

/* ── Streaming Invocation & Callback Types ────────────────────────────────── */
typedef enum cyrene_stream_event_type_v1 {
    CYRENE_STREAM_EVENT_DATA       = 0,  /* Incremental chunk / delta event */
    CYRENE_STREAM_EVENT_TERMINAL   = 1,  /* Final terminal completion event */
    CYRENE_STREAM_EVENT_CANCELLED  = 2,  /* Stream interrupted due to cancellation */
    CYRENE_STREAM_EVENT_ERROR      = 3   /* Stream failed with error */
} cyrene_stream_event_type_v1_t;

/**
 * @brief Streaming event delivered to user callback.
 * 
 * LIFETIME CONTRACT:
 * - All pointers inside cyrene_stream_event_v1_t (error_message, type_url, payload_data)
 *   are ONLY guaranteed to be valid during the callback execution.
 * - If the receiver needs to retain the data after callback returns, it MUST copy it.
 * - sequence_number starts at 0 and is strictly monotonically increasing per stream.
 * - Exactly one terminal event (is_terminal == true) will be delivered per stream invocation.
 */
typedef struct cyrene_stream_event_v1 {
    uint64_t sequence_number;        /* Monotonically increasing sequence number: 0, 1, 2, ... */
    int32_t event_type;              /* cyrene_stream_event_type_v1_t */
    int32_t status_code;             /* cyrene_status_code_v1_t */
    const char* error_message;       /* UTF-8 error string or NULL */
    const char* type_url;            /* UTF-8 type_url or NULL */
    const uint8_t* payload_data;     /* Protobuf chunk bytes or NULL */
    uint64_t payload_size;           /* Byte length */
    bool is_terminal;                /* true for final event (CYRENE_STREAM_EVENT_TERMINAL / CANCELLED / ERROR) */
} cyrene_stream_event_v1_t;

/**
 * @brief Function pointer signature for stream callbacks.
 * 流式事件回调函数原型。
 */
typedef void (*cyrene_stream_callback_v1_fn)(
    const cyrene_stream_event_v1_t* event,
    void* user_data
);

typedef struct cyrene_stream_request_v1 {
    const char* method;              /* Stream method name */
    const char* input_type_url;      /* Null-terminated UTF-8 type_url */
    const uint8_t* input_data;       /* Caller-allocated Protobuf wire bytes (read-only) */
    uint64_t input_size;             /* Byte length */
    uint64_t deadline_ms;            /* Wall-clock deadline in epoch milliseconds (0 = no deadline) */
} cyrene_stream_request_v1_t;

/* ═══════════════════════════════════════════════════════════════════════════ */
/*                        ABI V1 FUNCTION ENTRYPOINTS                          */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * ════════════════════════════════════════════════════════════════════════════
 * @brief Negotiates ABI, capabilities, and interface version with the plugin.
 * 
 * 协商 ABI 版本、能力列表及接口版本信息。
 * 
 * @param[in]  requested_abi_version Must be CYRENE_ABI_VERSION_1.
 * @param[out] out_info              Populated with plugin metadata and capabilities.
 * @return                           CYRENE_STATUS_OK on success, or error status code.
 * ════════════════════════════════════════════════════════════════════════════
 */
CYRENE_ABI_EXPORT int32_t cyrene_plugin_get_api_v1(
    uint32_t requested_abi_version,
    cyrene_api_info_v1_t* out_info
);

/**
 * ════════════════════════════════════════════════════════════════════════════
 * @brief Initializes and creates a new plugin instance.
 * 
 * 创建并初始化插件实例。
 * 
 * @param[in]  options    Initialization options, or NULL for defaults.
 * @param[out] out_handle Opaque handle to the newly allocated instance.
 * @return                CYRENE_STATUS_OK on success, or error status code.
 * ════════════════════════════════════════════════════════════════════════════
 */
CYRENE_ABI_EXPORT int32_t cyrene_plugin_create_v1(
    const cyrene_create_options_v1_t* options,
    cyrene_plugin_handle_t* out_handle
);

/**
 * ════════════════════════════════════════════════════════════════════════════
 * @brief Performs a synchronous blocking unary invocation.
 * 
 * 执行同步阻塞单次调用。
 * 
 * @param[in]  handle       Valid plugin instance handle.
 * @param[in]  request      Invocation payload containing method, type_url, bytes.
 * @param[out] out_response Destination struct populated with response data.
 *                          Buffers inside must be freed via cyrene_plugin_free_buffer_v1.
 * @return                  CYRENE_STATUS_OK on success, or error status code.
 * ════════════════════════════════════════════════════════════════════════════
 */
CYRENE_ABI_EXPORT int32_t cyrene_plugin_invoke_v1(
    cyrene_plugin_handle_t handle,
    const cyrene_invoke_request_v1_t* request,
    cyrene_invoke_response_v1_t* out_response
);

/**
 * ════════════════════════════════════════════════════════════════════════════
 * @brief Initiates a streaming invocation, delivering events through a callback.
 * 
 * 发起流式调用，通过回调函数依次投递事件。
 * 
 * @param[in]  handle            Valid plugin instance handle.
 * @param[in]  request           Stream request payload.
 * @param[in]  callback          Event consumer function.
 * @param[in]  user_data         Context passed to callback.
 * @param[out] out_cancel_handle Receives an opaque cancellation handle if non-NULL.
 * @return                       CYRENE_STATUS_OK on success, or error status code.
 * ════════════════════════════════════════════════════════════════════════════
 */
CYRENE_ABI_EXPORT int32_t cyrene_plugin_invoke_stream_v1(
    cyrene_plugin_handle_t handle,
    const cyrene_stream_request_v1_t* request,
    cyrene_stream_callback_v1_fn callback,
    void* user_data,
    cyrene_cancel_handle_t* out_cancel_handle
);

/**
 * ════════════════════════════════════════════════════════════════════════════
 * @brief Asynchronously requests cancellation of an ongoing stream operation.
 * 
 * 异步请求取消正在执行的流式调用。
 * 
 * @param[in] handle        Valid plugin instance handle.
 * @param[in] cancel_handle Valid cancellation handle returned from invoke_stream.
 * @return                  CYRENE_STATUS_OK on success, or error status code.
 * ════════════════════════════════════════════════════════════════════════════
 */
CYRENE_ABI_EXPORT int32_t cyrene_plugin_cancel_v1(
    cyrene_plugin_handle_t handle,
    cyrene_cancel_handle_t cancel_handle
);

/**
 * ════════════════════════════════════════════════════════════════════════════
 * @brief Releases a buffer allocated by this plugin library.
 * 
 * 释放由该插件库动态分配的内存缓冲区（遵循 Allocator Ownership）。
 * 
 * @param[in] handle Valid plugin instance handle.
 * @param[in] buffer Pointer to buffer previously returned by invoke/get_api.
 *                   If NULL, this function safely returns CYRENE_STATUS_OK.
 *                   If double-freed or foreign, returns CYRENE_STATUS_INVALID_ARGUMENT.
 * @return           CYRENE_STATUS_OK on success, or error status code.
 * ════════════════════════════════════════════════════════════════════════════
 */
CYRENE_ABI_EXPORT int32_t cyrene_plugin_free_buffer_v1(
    cyrene_plugin_handle_t handle,
    void* buffer
);

/**
 * ════════════════════════════════════════════════════════════════════════════
 * @brief Destroys a plugin instance and cleans up all allocated resources.
 * 
 * 销毁插件实例并清理其持有的全部系统资源。
 * 
 * @param[in] handle Valid plugin instance handle.
 * @return           CYRENE_STATUS_OK on success, or error status code.
 * ════════════════════════════════════════════════════════════════════════════
 */
CYRENE_ABI_EXPORT int32_t cyrene_plugin_destroy_v1(
    cyrene_plugin_handle_t handle
);

#ifdef __cplusplus
}
#endif

#endif /* CYRENE_PLUGIN_ABI_V1_H */
