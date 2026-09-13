// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 ComputerClient.java                                             │
// │  Package: io.cyrene.plugin.client.typed                             │
// │  Role: Typed client for Computer Runtime (T43, R05).                │
// │                                                                     │
// │  模块职责：类型化 Computer 客户端，支持命令执行、文件操作与构建物传输   │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.typed;

import com.google.protobuf.InvalidProtocolBufferException;
import io.cyrene.plugin.client.context.InvocationContext;
import io.cyrene.plugin.client.runtime.DirectPluginRuntimeClient;
import io.cyrene.plugin.runtime.v1.DirectInvocationRequest;
import io.cyrene.plugin.runtime.v1.DirectInvocationResponse;
import io.cyrene.plugin.runtime.v1.DirectStreamMode;
import io.cyrene.proto.computer.runtime.v1.CommandExecutionRequest;
import io.cyrene.proto.computer.runtime.v1.CommandExecutionResponse;
import io.cyrene.proto.computer.runtime.v1.CreateArtifactRequest;
import io.cyrene.proto.computer.runtime.v1.CreateArtifactResponse;
import io.cyrene.proto.computer.runtime.v1.GetArtifactRequest;
import io.cyrene.proto.computer.runtime.v1.GetArtifactResponse;
import io.cyrene.proto.computer.runtime.v1.ListDirRequest;
import io.cyrene.proto.computer.runtime.v1.ListDirResponse;
import io.cyrene.proto.computer.runtime.v1.ReadFileRequest;
import io.cyrene.proto.computer.runtime.v1.ReadFileResponse;
import io.cyrene.proto.computer.runtime.v1.WriteFileRequest;
import io.cyrene.proto.computer.runtime.v1.WriteFileResponse;

import java.util.Objects;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Strongly-typed client for computer.runtime.v1 capability (T43, R05).
 * ════════════════════════════════════════════════════════════════════════
 */
public final class ComputerClient {

    public static final String CAPABILITY = "computer.runtime.v1";
    public static final String INTERFACE_VERSION = "1";

    public static final String METHOD_EXECUTE_COMMAND = "ExecuteCommand";
    public static final String METHOD_READ_FILE = "ReadFile";
    public static final String METHOD_WRITE_FILE = "WriteFile";
    public static final String METHOD_LIST_DIR = "ListDir";
    public static final String METHOD_CREATE_ARTIFACT = "CreateArtifact";
    public static final String METHOD_GET_ARTIFACT = "GetArtifact";

    public static final String TYPE_URL_COMMAND = "type.cyrene.io/cyrene.computer.runtime.v1.CommandExecutionRequest";
    public static final String TYPE_URL_READ_FILE = "type.cyrene.io/cyrene.computer.runtime.v1.ReadFileRequest";
    public static final String TYPE_URL_WRITE_FILE = "type.cyrene.io/cyrene.computer.runtime.v1.WriteFileRequest";
    public static final String TYPE_URL_LIST_DIR = "type.cyrene.io/cyrene.computer.runtime.v1.ListDirRequest";
    public static final String TYPE_URL_CREATE_ARTIFACT = "type.cyrene.io/cyrene.computer.runtime.v1.CreateArtifactRequest";
    public static final String TYPE_URL_GET_ARTIFACT = "type.cyrene.io/cyrene.computer.runtime.v1.GetArtifactRequest";

    private final DirectPluginRuntimeClient runtimeClient;

    public ComputerClient(DirectPluginRuntimeClient runtimeClient) {
        this.runtimeClient = Objects.requireNonNull(runtimeClient, "runtimeClient must not be null");
    }

    public CommandExecutionResponse executeCommand(String bindingId, CommandExecutionRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_EXECUTE_COMMAND)
            .setPayloadTypeUrl(TYPE_URL_COMMAND)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        DirectInvocationResponse resp = runtimeClient.invoke(bindingId, dir, context);
        if (resp.hasError()) {
            throw new RuntimeException("ExecuteCommand failed: [" + resp.getError().getCode() + "] " + resp.getError().getMessage());
        }
        try {
            return CommandExecutionResponse.parseFrom(resp.getPayload().getValue());
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException("Failed to parse CommandExecutionResponse", e);
        }
    }

    public ReadFileResponse readFile(String bindingId, ReadFileRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_READ_FILE)
            .setPayloadTypeUrl(TYPE_URL_READ_FILE)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        DirectInvocationResponse resp = runtimeClient.invoke(bindingId, dir, context);
        if (resp.hasError()) {
            throw new RuntimeException("ReadFile failed: [" + resp.getError().getCode() + "] " + resp.getError().getMessage());
        }
        try {
            return ReadFileResponse.parseFrom(resp.getPayload().getValue());
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException("Failed to parse ReadFileResponse", e);
        }
    }

    public WriteFileResponse writeFile(String bindingId, WriteFileRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_WRITE_FILE)
            .setPayloadTypeUrl(TYPE_URL_WRITE_FILE)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        DirectInvocationResponse resp = runtimeClient.invoke(bindingId, dir, context);
        if (resp.hasError()) {
            throw new RuntimeException("WriteFile failed: [" + resp.getError().getCode() + "] " + resp.getError().getMessage());
        }
        try {
            return WriteFileResponse.parseFrom(resp.getPayload().getValue());
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException("Failed to parse WriteFileResponse", e);
        }
    }

    public CreateArtifactResponse createArtifact(String bindingId, CreateArtifactRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_CREATE_ARTIFACT)
            .setPayloadTypeUrl(TYPE_URL_CREATE_ARTIFACT)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        DirectInvocationResponse resp = runtimeClient.invoke(bindingId, dir, context);
        if (resp.hasError()) {
            throw new RuntimeException("CreateArtifact failed: [" + resp.getError().getCode() + "] " + resp.getError().getMessage());
        }
        try {
            return CreateArtifactResponse.parseFrom(resp.getPayload().getValue());
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException("Failed to parse CreateArtifactResponse", e);
        }
    }

    public GetArtifactResponse getArtifact(String bindingId, GetArtifactRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_GET_ARTIFACT)
            .setPayloadTypeUrl(TYPE_URL_GET_ARTIFACT)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        DirectInvocationResponse resp = runtimeClient.invoke(bindingId, dir, context);
        if (resp.hasError()) {
            throw new RuntimeException("GetArtifact failed: [" + resp.getError().getCode() + "] " + resp.getError().getMessage());
        }
        try {
            return GetArtifactResponse.parseFrom(resp.getPayload().getValue());
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException("Failed to parse GetArtifactResponse", e);
        }
    }
}
