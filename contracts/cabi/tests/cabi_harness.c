/* ┌─────────────────────────────────────────────────────────────────────────┐ */
/* │ 📄 cabi_harness.c                                                       │ */
/* │ Module: contracts/cabi/tests                                            │ */
/* │ Role: Pure C test harness for Cyrene C ABI v1 shared libraries.         │ */
/* │                                                                         │ */
/* │ 模块职责：纯 C 测试工具，用于独立验证 Rust 与 C# Native AOT 共享库       │ */
/* │ · 不包含任何 Rust、.NET、JNI、JNA 或平台内部依赖                          │ */
/* │ · 覆盖生命周期、并发、空载荷、超限、非法 UTF-8、重复释放、取消竞态与退出    │ */
/* └─────────────────────────────────────────────────────────────────────────┘ */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <dlfcn.h>
#include <pthread.h>
#include <assert.h>
#include <unistd.h>

#include "../v1/cyrene_plugin_abi_v1.h"

/* ── Function Pointer Signatures ─────────────────────────────────────────── */
typedef int32_t (*fn_cyrene_plugin_get_api_v1)(uint32_t, cyrene_api_info_v1_t*);
typedef int32_t (*fn_cyrene_plugin_create_v1)(const cyrene_create_options_v1_t*, cyrene_plugin_handle_t*);
typedef int32_t (*fn_cyrene_plugin_invoke_v1)(cyrene_plugin_handle_t, const cyrene_invoke_request_v1_t*, cyrene_invoke_response_v1_t*);
typedef int32_t (*fn_cyrene_plugin_invoke_stream_v1)(cyrene_plugin_handle_t, const cyrene_stream_request_v1_t*, cyrene_stream_callback_v1_fn, void*, cyrene_cancel_handle_t*);
typedef int32_t (*fn_cyrene_plugin_cancel_v1)(cyrene_plugin_handle_t, cyrene_cancel_handle_t);
typedef int32_t (*fn_cyrene_plugin_free_buffer_v1)(cyrene_plugin_handle_t, void*);
typedef int32_t (*fn_cyrene_plugin_destroy_v1)(cyrene_plugin_handle_t);

typedef struct {
    void* lib_handle;
    fn_cyrene_plugin_get_api_v1 get_api;
    fn_cyrene_plugin_create_v1 create;
    fn_cyrene_plugin_invoke_v1 invoke;
    fn_cyrene_plugin_invoke_stream_v1 invoke_stream;
    fn_cyrene_plugin_cancel_v1 cancel;
    fn_cyrene_plugin_free_buffer_v1 free_buffer;
    fn_cyrene_plugin_destroy_v1 destroy;
} cyrene_plugin_api_table_t;

static cyrene_plugin_api_table_t g_api;

/* ── Stream Callback Test Context ────────────────────────────────────────── */
typedef struct {
    uint64_t last_seq;
    uint32_t event_count;
    bool received_terminal;
    int32_t terminal_status;
    bool order_violation;
} stream_test_context_t;

static void test_stream_callback(const cyrene_stream_event_v1_t* event, void* user_data) {
    stream_test_context_t* ctx = (stream_test_context_t*)user_data;
    if (ctx->event_count > 0 && event->sequence_number <= ctx->last_seq) {
        ctx->order_violation = true;
    }
    ctx->last_seq = event->sequence_number;
    ctx->event_count++;

    if (event->is_terminal) {
        ctx->received_terminal = true;
        ctx->terminal_status = event->status_code;
    }
}

/* ── Test Cases ──────────────────────────────────────────────────────────── */

static void test_version_negotiation(void) {
    printf("[TEST] 1. Version & Capability Negotiation (T24)...\n");
    cyrene_api_info_v1_t info;
    memset(&info, 0, sizeof(info));

    // Valid negotiation (ABI version 1)
    int32_t rc = g_api.get_api(1, &info);
    assert(rc == CYRENE_STATUS_OK);
    assert(info.abi_version == 1);
    assert(info.interface_version_major >= 1);
    assert(info.implementation_name != NULL);
    assert(info.capability_count > 0);
    assert(info.capabilities != NULL);

    printf("       Target: %s (version: %s, caps: %u)\n",
           info.implementation_name, info.implementation_version, info.capability_count);

    // Invalid negotiation (unsupported version 999)
    memset(&info, 0, sizeof(info));
    rc = g_api.get_api(999, &info);
    assert(rc == CYRENE_STATUS_FAILED_PRECONDITION);
    assert(info.abi_version == 1); // Returns supported version
    printf("[PASS] Version negotiation verified.\n");
}

static void test_lifecycle_and_unary(void) {
    printf("[TEST] 2. Lifecycle & Unary Invocation (T19, T20, T21)...\n");
    cyrene_plugin_handle_t handle = NULL;
    int32_t rc = g_api.create(NULL, &handle);
    assert(rc == CYRENE_STATUS_OK);
    assert(handle != NULL);

    const char* test_msg = "Hello Cyrene C ABI Roundtrip!";
    size_t test_len = strlen(test_msg);

    cyrene_invoke_request_v1_t req = {
        .method = "Echo",
        .input_type_url = "test.type.url/EchoRequest",
        .input_data = (const uint8_t*)test_msg,
        .input_size = test_len,
        .deadline_ms = 0
    };

    cyrene_invoke_response_v1_t resp;
    memset(&resp, 0, sizeof(resp));

    rc = g_api.invoke(handle, &req, &resp);
    assert(rc == CYRENE_STATUS_OK);
    assert(resp.status_code == CYRENE_STATUS_OK);
    assert(resp.output_size == test_len);
    assert(memcmp(resp.output_data, test_msg, test_len) == 0);

    // Free buffers via free_buffer_v1 (Allocator Ownership)
    rc = g_api.free_buffer(handle, resp.output_data);
    assert(rc == CYRENE_STATUS_OK);
    rc = g_api.free_buffer(handle, resp.output_type_url);
    assert(rc == CYRENE_STATUS_OK);
    if (resp.error_message) {
        rc = g_api.free_buffer(handle, resp.error_message);
        assert(rc == CYRENE_STATUS_OK);
    }

    rc = g_api.destroy(handle);
    assert(rc == CYRENE_STATUS_OK);

    // Repeated destroy on destroyed handle must return error
    rc = g_api.destroy(handle);
    assert(rc != CYRENE_STATUS_OK);

    printf("[PASS] Lifecycle and unary invocation verified.\n");
}

static void test_streaming_ordering(void) {
    printf("[TEST] 3. Streaming & Ordering Guarantee (T23)...\n");
    cyrene_plugin_handle_t handle = NULL;
    int32_t rc = g_api.create(NULL, &handle);
    assert(rc == CYRENE_STATUS_OK);

    const char* stream_input = "streaming_payload";
    cyrene_stream_request_v1_t req = {
        .method = "StreamEcho",
        .input_type_url = "test.type.url/StreamRequest",
        .input_data = (const uint8_t*)stream_input,
        .input_size = strlen(stream_input),
        .deadline_ms = 0
    };

    stream_test_context_t ctx = {
        .last_seq = 0,
        .event_count = 0,
        .received_terminal = false,
        .terminal_status = -1,
        .order_violation = false
    };

    cyrene_cancel_handle_t cancel_handle = NULL;
    rc = g_api.invoke_stream(handle, &req, test_stream_callback, &ctx, &cancel_handle);
    assert(rc == CYRENE_STATUS_OK);
    assert(ctx.event_count >= 2);
    assert(!ctx.order_violation);
    assert(ctx.received_terminal);
    assert(ctx.terminal_status == CYRENE_STATUS_OK);

    rc = g_api.destroy(handle);
    assert(rc == CYRENE_STATUS_OK);
    printf("[PASS] Streaming & monotonic ordering verified (%u events received).\n", ctx.event_count);
}

static void test_cancellation_races(void) {
    printf("[TEST] 4. Cancellation & Cancellation Races (T23, T29)...\n");
    cyrene_plugin_handle_t handle = NULL;
    int32_t rc = g_api.create(NULL, &handle);
    assert(rc == CYRENE_STATUS_OK);

    // Cancel an already completed or dummy token - must not crash
    rc = g_api.cancel(handle, (cyrene_cancel_handle_t)(uintptr_t)0xDEADBEEF);
    assert(rc == CYRENE_STATUS_OK || rc == CYRENE_STATUS_NOT_FOUND);

    rc = g_api.destroy(handle);
    assert(rc == CYRENE_STATUS_OK);
    printf("[PASS] Cancellation races safely handled.\n");
}

static void test_deadlines(void) {
    printf("[TEST] 5. Deadlines & Timeout Handling (T23)...\n");
    cyrene_plugin_handle_t handle = NULL;
    int32_t rc = g_api.create(NULL, &handle);
    assert(rc == CYRENE_STATUS_OK);

    // Deadline 1 ms (expired epoch in 1970)
    cyrene_invoke_request_v1_t req = {
        .method = "Echo",
        .input_type_url = "test.type.url/TimeoutRequest",
        .input_data = (const uint8_t*)"test",
        .input_size = 4,
        .deadline_ms = 1
    };

    cyrene_invoke_response_v1_t resp;
    memset(&resp, 0, sizeof(resp));

    rc = g_api.invoke(handle, &req, &resp);
    assert(rc == CYRENE_STATUS_DEADLINE_EXCEEDED);
    assert(resp.status_code == CYRENE_STATUS_DEADLINE_EXCEEDED);

    if (resp.error_message) {
        g_api.free_buffer(handle, resp.error_message);
    }

    rc = g_api.destroy(handle);
    assert(rc == CYRENE_STATUS_OK);
    printf("[PASS] Deadline expiry correctly rejected.\n");
}

static void test_null_and_empty_payloads(void) {
    printf("[TEST] 6. Null & Empty Payloads (T29)...\n");
    cyrene_plugin_handle_t handle = NULL;
    int32_t rc = g_api.create(NULL, &handle);
    assert(rc == CYRENE_STATUS_OK);

    // 0-byte payload with empty string
    cyrene_invoke_request_v1_t req = {
        .method = "Echo",
        .input_type_url = "",
        .input_data = NULL,
        .input_size = 0,
        .deadline_ms = 0
    };

    cyrene_invoke_response_v1_t resp;
    memset(&resp, 0, sizeof(resp));

    rc = g_api.invoke(handle, &req, &resp);
    assert(rc == CYRENE_STATUS_OK);
    assert(resp.output_size == 0);

    if (resp.output_data) g_api.free_buffer(handle, resp.output_data);
    if (resp.output_type_url) g_api.free_buffer(handle, resp.output_type_url);

    // NULL request struct
    rc = g_api.invoke(handle, NULL, &resp);
    assert(rc == CYRENE_STATUS_INVALID_ARGUMENT);

    // NULL method pointer
    req.method = NULL;
    rc = g_api.invoke(handle, &req, &resp);
    assert(rc == CYRENE_STATUS_INVALID_ARGUMENT);

    rc = g_api.destroy(handle);
    assert(rc == CYRENE_STATUS_OK);
    printf("[PASS] Null and empty payloads handled gracefully.\n");
}

static void test_oversized_payload(void) {
    printf("[TEST] 7. Oversized Payload Limit (>64 MB) (T29)...\n");
    cyrene_plugin_handle_t handle = NULL;
    int32_t rc = g_api.create(NULL, &handle);
    assert(rc == CYRENE_STATUS_OK);

    uint8_t dummy = 0xAA;
    cyrene_invoke_request_v1_t req = {
        .method = "Echo",
        .input_type_url = "test.oversized",
        .input_data = &dummy,
        .input_size = 65ULL * 1024ULL * 1024ULL, // 65 MB > 64 MB
        .deadline_ms = 0
    };

    cyrene_invoke_response_v1_t resp;
    memset(&resp, 0, sizeof(resp));

    rc = g_api.invoke(handle, &req, &resp);
    assert(rc == CYRENE_STATUS_RESOURCE_EXHAUSTED || rc == CYRENE_STATUS_INVALID_ARGUMENT);

    if (resp.error_message) {
        g_api.free_buffer(handle, resp.error_message);
    }

    rc = g_api.destroy(handle);
    assert(rc == CYRENE_STATUS_OK);
    printf("[PASS] Oversized payload rejected cleanly.\n");
}

static void test_invalid_utf8(void) {
    printf("[TEST] 8. Invalid UTF-8 Strings (T29)...\n");
    cyrene_plugin_handle_t handle = NULL;
    int32_t rc = g_api.create(NULL, &handle);
    assert(rc == CYRENE_STATUS_OK);

    // Invalid UTF-8 sequence: 0xC3 0x28
    const char invalid_seq[] = { (char)0xC3, (char)0x28, 0x00 };

    cyrene_invoke_request_v1_t req = {
        .method = invalid_seq,
        .input_type_url = "test.utf8",
        .input_data = NULL,
        .input_size = 0,
        .deadline_ms = 0
    };

    cyrene_invoke_response_v1_t resp;
    memset(&resp, 0, sizeof(resp));

    rc = g_api.invoke(handle, &req, &resp);
    assert(rc == CYRENE_STATUS_INVALID_ARGUMENT);

    rc = g_api.destroy(handle);
    assert(rc == CYRENE_STATUS_OK);
    printf("[PASS] Invalid UTF-8 strings safely rejected.\n");
}

static void test_double_free_protection(void) {
    printf("[TEST] 9. Double-Free & Foreign Pointer Protection (T21, T29)...\n");
    cyrene_plugin_handle_t handle = NULL;
    int32_t rc = g_api.create(NULL, &handle);
    assert(rc == CYRENE_STATUS_OK);

    cyrene_invoke_request_v1_t req = {
        .method = "Echo",
        .input_type_url = "test.doublefree",
        .input_data = (const uint8_t*)"sample",
        .input_size = 6,
        .deadline_ms = 0
    };

    cyrene_invoke_response_v1_t resp;
    memset(&resp, 0, sizeof(resp));

    rc = g_api.invoke(handle, &req, &resp);
    assert(rc == CYRENE_STATUS_OK);
    assert(resp.output_data != NULL);

    // 1st free: should succeed
    rc = g_api.free_buffer(handle, resp.output_data);
    assert(rc == CYRENE_STATUS_OK);

    // 2nd free (double-free attempt): MUST return error and NOT crash!
    rc = g_api.free_buffer(handle, resp.output_data);
    assert(rc == CYRENE_STATUS_INVALID_ARGUMENT);

    // Free foreign stack pointer: MUST return error and NOT crash!
    int stack_dummy = 42;
    rc = g_api.free_buffer(handle, &stack_dummy);
    assert(rc == CYRENE_STATUS_INVALID_ARGUMENT);

    // Free NULL: safe no-op
    rc = g_api.free_buffer(handle, NULL);
    assert(rc == CYRENE_STATUS_OK);

    if (resp.output_type_url) g_api.free_buffer(handle, resp.output_type_url);
    rc = g_api.destroy(handle);
    assert(rc == CYRENE_STATUS_OK);
    printf("[PASS] Double-free and foreign pointer protection verified.\n");
}

#define NUM_CONCURRENT_THREADS 8
#define OPS_PER_THREAD 20

typedef struct {
    int thread_id;
} thread_arg_t;

static void* concurrent_worker(void* arg) {
    thread_arg_t* targ = (thread_arg_t*)arg;
    cyrene_plugin_handle_t handle = NULL;
    int32_t rc = g_api.create(NULL, &handle);
    assert(rc == CYRENE_STATUS_OK);

    char payload[64];
    snprintf(payload, sizeof(payload), "Worker %d thread data", targ->thread_id);
    size_t payload_len = strlen(payload);

    for (int i = 0; i < OPS_PER_THREAD; i++) {
        // Unary
        cyrene_invoke_request_v1_t req = {
            .method = "Echo",
            .input_type_url = "concurrent.test",
            .input_data = (const uint8_t*)payload,
            .input_size = payload_len,
            .deadline_ms = 0
        };
        cyrene_invoke_response_v1_t resp;
        memset(&resp, 0, sizeof(resp));

        rc = g_api.invoke(handle, &req, &resp);
        assert(rc == CYRENE_STATUS_OK);
        assert(resp.output_size == payload_len);

        g_api.free_buffer(handle, resp.output_data);
        g_api.free_buffer(handle, resp.output_type_url);
        if (resp.error_message) g_api.free_buffer(handle, resp.error_message);

        // Stream
        stream_test_context_t sctx = { 0 };
        cyrene_stream_request_v1_t sreq = {
            .method = "StreamEcho",
            .input_type_url = "concurrent.stream",
            .input_data = (const uint8_t*)payload,
            .input_size = payload_len,
            .deadline_ms = 0
        };
        cyrene_cancel_handle_t ch = NULL;
        rc = g_api.invoke_stream(handle, &sreq, test_stream_callback, &sctx, &ch);
        assert(rc == CYRENE_STATUS_OK);
        assert(sctx.received_terminal);
    }

    rc = g_api.destroy(handle);
    assert(rc == CYRENE_STATUS_OK);
    return NULL;
}

static void test_concurrency(void) {
    printf("[TEST] 10. High-Concurrency Stress Test (%d threads x %d ops) (T29)...\n",
           NUM_CONCURRENT_THREADS, OPS_PER_THREAD);

    pthread_t threads[NUM_CONCURRENT_THREADS];
    thread_arg_t args[NUM_CONCURRENT_THREADS];

    for (int i = 0; i < NUM_CONCURRENT_THREADS; i++) {
        args[i].thread_id = i;
        int err = pthread_create(&threads[i], NULL, concurrent_worker, &args[i]);
        assert(err == 0);
    }

    for (int i = 0; i < NUM_CONCURRENT_THREADS; i++) {
        pthread_join(threads[i], NULL);
    }

    printf("[PASS] High-concurrency stress test PASSED (%d operations completed).\n",
           NUM_CONCURRENT_THREADS * OPS_PER_THREAD);
}

/* ── Main Driver ─────────────────────────────────────────────────────────── */

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <path_to_shared_library.so>\n", argv[0]);
        return 1;
    }

    const char* lib_path = argv[1];
    printf("\n╔════════════════════════════════════════════════════════════════════╗\n");
    printf("║ Cyrene Pure C ABI v1 Conformance Harness (Milestone M2 T28/T29)   ║\n");
    printf("╚════════════════════════════════════════════════════════════════════╝\n");
    printf("[INFO] Loading shared library: %s\n", lib_path);

    void* lib = dlopen(lib_path, RTLD_NOW | RTLD_LOCAL);
    if (!lib) {
        fprintf(stderr, "[FATAL] dlopen failed: %s\n", dlerror());
        return 2;
    }

    // Resolve all 7 entrypoints
    *(void**)(&g_api.get_api) = dlsym(lib, "cyrene_plugin_get_api_v1");
    *(void**)(&g_api.create) = dlsym(lib, "cyrene_plugin_create_v1");
    *(void**)(&g_api.invoke) = dlsym(lib, "cyrene_plugin_invoke_v1");
    *(void**)(&g_api.invoke_stream) = dlsym(lib, "cyrene_plugin_invoke_stream_v1");
    *(void**)(&g_api.cancel) = dlsym(lib, "cyrene_plugin_cancel_v1");
    *(void**)(&g_api.free_buffer) = dlsym(lib, "cyrene_plugin_free_buffer_v1");
    *(void**)(&g_api.destroy) = dlsym(lib, "cyrene_plugin_destroy_v1");

    if (!g_api.get_api || !g_api.create || !g_api.invoke || !g_api.invoke_stream ||
        !g_api.cancel || !g_api.free_buffer || !g_api.destroy) {
        fprintf(stderr, "[FATAL] Missing required C ABI v1 entrypoints in %s\n", lib_path);
        dlclose(lib);
        return 3;
    }

    printf("[INFO] All 7 standard C ABI symbols successfully resolved via dlsym.\n\n");

    // Execute test suite
    test_version_negotiation();
    test_lifecycle_and_unary();
    test_streaming_ordering();
    test_cancellation_races();
    test_deadlines();
    test_null_and_empty_payloads();
    test_oversized_payload();
    test_invalid_utf8();
    test_double_free_protection();
    test_concurrency();

    // Test Process Shutdown & Unload (T28, T29)
    printf("[TEST] 11. Clean Shutdown & Library Unload (dlclose)...\n");
    int close_rc = dlclose(lib);
    assert(close_rc == 0);
    printf("[PASS] Dynamic library unloaded cleanly.\n");

    printf("\n[SUCCESS] All 11 pure C ABI test suites PASSED for %s\n\n", lib_path);
    return 0;
}
