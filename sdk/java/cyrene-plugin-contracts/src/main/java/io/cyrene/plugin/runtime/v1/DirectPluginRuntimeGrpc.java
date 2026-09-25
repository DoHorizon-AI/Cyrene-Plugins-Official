// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 DirectPluginRuntimeGrpc.java                                    │
// │  Package: io.cyrene.plugin.runtime.v1                               │
// │  Role: gRPC Service stub for DirectPluginRuntime (T42).            │
// │                                                                     │
// │  模块职责：生成 async、blocking 和 future DirectPluginRuntime stubs │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.runtime.v1;

import static io.grpc.MethodDescriptor.generateFullMethodName;

/**
 * ════════════════════════════════════════════════════════════════════════
 * gRPC service stub and method descriptors for DirectPluginRuntime.
 *
 * <p>提供异步、阻塞式和 Future 风格的 DirectPluginRuntime 服务存根。</p>
 * ════════════════════════════════════════════════════════════════════════
 */
public final class DirectPluginRuntimeGrpc {

    private DirectPluginRuntimeGrpc() {}

    public static final String SERVICE_NAME = "cyrene.plugin.runtime.v1.DirectPluginRuntime";

    // ── Method Descriptors ─────────────────────────────────────────────── | 中文：方法描述符

    private static volatile io.grpc.MethodDescriptor<DirectInvocationRequest, DirectInvocationResponse> getInvokeMethod;

    public static io.grpc.MethodDescriptor<DirectInvocationRequest, DirectInvocationResponse> getInvokeMethod() {
        io.grpc.MethodDescriptor<DirectInvocationRequest, DirectInvocationResponse> getInvokeMethod;
        if ((getInvokeMethod = DirectPluginRuntimeGrpc.getInvokeMethod) == null) {
            synchronized (DirectPluginRuntimeGrpc.class) {
                if ((getInvokeMethod = DirectPluginRuntimeGrpc.getInvokeMethod) == null) {
                    DirectPluginRuntimeGrpc.getInvokeMethod = getInvokeMethod =
                        io.grpc.MethodDescriptor.<DirectInvocationRequest, DirectInvocationResponse>newBuilder()
                            .setType(io.grpc.MethodDescriptor.MethodType.UNARY)
                            .setFullMethodName(generateFullMethodName(SERVICE_NAME, "Invoke"))
                            .setSampledToLocalTracing(true)
                            .setRequestMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(DirectInvocationRequest.getDefaultInstance()))
                            .setResponseMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(DirectInvocationResponse.getDefaultInstance()))
                            .setSchemaDescriptor(null)
                            .build();
                }
            }
        }
        return getInvokeMethod;
    }

    private static volatile io.grpc.MethodDescriptor<DirectInvocationRequest, DirectStreamItem> getInvokeStreamMethod;

    public static io.grpc.MethodDescriptor<DirectInvocationRequest, DirectStreamItem> getInvokeStreamMethod() {
        io.grpc.MethodDescriptor<DirectInvocationRequest, DirectStreamItem> getInvokeStreamMethod;
        if ((getInvokeStreamMethod = DirectPluginRuntimeGrpc.getInvokeStreamMethod) == null) {
            synchronized (DirectPluginRuntimeGrpc.class) {
                if ((getInvokeStreamMethod = DirectPluginRuntimeGrpc.getInvokeStreamMethod) == null) {
                    DirectPluginRuntimeGrpc.getInvokeStreamMethod = getInvokeStreamMethod =
                        io.grpc.MethodDescriptor.<DirectInvocationRequest, DirectStreamItem>newBuilder()
                            .setType(io.grpc.MethodDescriptor.MethodType.SERVER_STREAMING)
                            .setFullMethodName(generateFullMethodName(SERVICE_NAME, "InvokeStream"))
                            .setSampledToLocalTracing(true)
                            .setRequestMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(DirectInvocationRequest.getDefaultInstance()))
                            .setResponseMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(DirectStreamItem.getDefaultInstance()))
                            .setSchemaDescriptor(null)
                            .build();
                }
            }
        }
        return getInvokeStreamMethod;
    }

    private static volatile io.grpc.MethodDescriptor<HealthRequest, HealthResponse> getHealthMethod;

    public static io.grpc.MethodDescriptor<HealthRequest, HealthResponse> getHealthMethod() {
        io.grpc.MethodDescriptor<HealthRequest, HealthResponse> getHealthMethod;
        if ((getHealthMethod = DirectPluginRuntimeGrpc.getHealthMethod) == null) {
            synchronized (DirectPluginRuntimeGrpc.class) {
                if ((getHealthMethod = DirectPluginRuntimeGrpc.getHealthMethod) == null) {
                    DirectPluginRuntimeGrpc.getHealthMethod = getHealthMethod =
                        io.grpc.MethodDescriptor.<HealthRequest, HealthResponse>newBuilder()
                            .setType(io.grpc.MethodDescriptor.MethodType.UNARY)
                            .setFullMethodName(generateFullMethodName(SERVICE_NAME, "Health"))
                            .setSampledToLocalTracing(true)
                            .setRequestMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(HealthRequest.getDefaultInstance()))
                            .setResponseMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(HealthResponse.getDefaultInstance()))
                            .setSchemaDescriptor(null)
                            .build();
                }
            }
        }
        return getHealthMethod;
    }

    // ── Stub Factories ─────────────────────────────────────────────────── | 中文：存根工厂

    public static DirectPluginRuntimeStub newStub(io.grpc.Channel channel) {
        return DirectPluginRuntimeStub.newStub(FACTORY_ASYNC, channel);
    }

    public static DirectPluginRuntimeBlockingStub newBlockingStub(io.grpc.Channel channel) {
        return DirectPluginRuntimeBlockingStub.newStub(FACTORY_BLOCKING, channel);
    }

    public static DirectPluginRuntimeFutureStub newFutureStub(io.grpc.Channel channel) {
        return DirectPluginRuntimeFutureStub.newStub(FACTORY_FUTURE, channel);
    }

    private static final io.grpc.stub.AbstractStub.StubFactory<DirectPluginRuntimeStub> FACTORY_ASYNC =
        new io.grpc.stub.AbstractStub.StubFactory<>() {
            @Override
            public DirectPluginRuntimeStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
                return new DirectPluginRuntimeStub(channel, callOptions);
            }
        };

    private static final io.grpc.stub.AbstractStub.StubFactory<DirectPluginRuntimeBlockingStub> FACTORY_BLOCKING =
        new io.grpc.stub.AbstractStub.StubFactory<>() {
            @Override
            public DirectPluginRuntimeBlockingStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
                return new DirectPluginRuntimeBlockingStub(channel, callOptions);
            }
        };

    private static final io.grpc.stub.AbstractStub.StubFactory<DirectPluginRuntimeFutureStub> FACTORY_FUTURE =
        new io.grpc.stub.AbstractStub.StubFactory<>() {
            @Override
            public DirectPluginRuntimeFutureStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
                return new DirectPluginRuntimeFutureStub(channel, callOptions);
            }
        };

    // ── Stub Implementations (T42) ─────────────────────────────────────── | 中文：存根实现（T42）

    /**
     * Async stub for DirectPluginRuntime.
     * 中文：DirectPluginRuntime 的异步存根。
     */
    public static final class DirectPluginRuntimeStub extends io.grpc.stub.AbstractAsyncStub<DirectPluginRuntimeStub> {
        private DirectPluginRuntimeStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
            super(channel, callOptions);
        }

        @Override
        protected DirectPluginRuntimeStub build(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
            return new DirectPluginRuntimeStub(channel, callOptions);
        }

        public void invoke(DirectInvocationRequest request, io.grpc.stub.StreamObserver<DirectInvocationResponse> responseObserver) {
            io.grpc.stub.ClientCalls.asyncUnaryCall(
                getChannel().newCall(getInvokeMethod(), getCallOptions()), request, responseObserver);
        }

        public void invokeStream(DirectInvocationRequest request, io.grpc.stub.StreamObserver<DirectStreamItem> responseObserver) {
            io.grpc.stub.ClientCalls.asyncServerStreamingCall(
                getChannel().newCall(getInvokeStreamMethod(), getCallOptions()), request, responseObserver);
        }

        public void health(HealthRequest request, io.grpc.stub.StreamObserver<HealthResponse> responseObserver) {
            io.grpc.stub.ClientCalls.asyncUnaryCall(
                getChannel().newCall(getHealthMethod(), getCallOptions()), request, responseObserver);
        }
    }

    /**
     * Blocking stub for DirectPluginRuntime.
     * 中文：DirectPluginRuntime 的阻塞式存根。
     */
    public static final class DirectPluginRuntimeBlockingStub extends io.grpc.stub.AbstractBlockingStub<DirectPluginRuntimeBlockingStub> {
        private DirectPluginRuntimeBlockingStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
            super(channel, callOptions);
        }

        @Override
        protected DirectPluginRuntimeBlockingStub build(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
            return new DirectPluginRuntimeBlockingStub(channel, callOptions);
        }

        public DirectInvocationResponse invoke(DirectInvocationRequest request) {
            return io.grpc.stub.ClientCalls.blockingUnaryCall(
                getChannel(), getInvokeMethod(), getCallOptions(), request);
        }

        public java.util.Iterator<DirectStreamItem> invokeStream(DirectInvocationRequest request) {
            return io.grpc.stub.ClientCalls.blockingServerStreamingCall(
                getChannel(), getInvokeStreamMethod(), getCallOptions(), request);
        }

        public HealthResponse health(HealthRequest request) {
            return io.grpc.stub.ClientCalls.blockingUnaryCall(
                getChannel(), getHealthMethod(), getCallOptions(), request);
        }
    }

    /**
     * Future stub for DirectPluginRuntime.
     * 中文：DirectPluginRuntime 的 Future 存根。
     */
    public static final class DirectPluginRuntimeFutureStub extends io.grpc.stub.AbstractFutureStub<DirectPluginRuntimeFutureStub> {
        private DirectPluginRuntimeFutureStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
            super(channel, callOptions);
        }

        @Override
        protected DirectPluginRuntimeFutureStub build(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
            return new DirectPluginRuntimeFutureStub(channel, callOptions);
        }

        public com.google.common.util.concurrent.ListenableFuture<DirectInvocationResponse> invoke(DirectInvocationRequest request) {
            return io.grpc.stub.ClientCalls.futureUnaryCall(
                getChannel().newCall(getInvokeMethod(), getCallOptions()), request);
        }

        public com.google.common.util.concurrent.ListenableFuture<HealthResponse> health(HealthRequest request) {
            return io.grpc.stub.ClientCalls.futureUnaryCall(
                getChannel().newCall(getHealthMethod(), getCallOptions()), request);
        }
    }

    // ── Server-side Base Implementation ────────────────────────────────── | 中文：服务端基础实现

    public static abstract class DirectPluginRuntimeImplBase implements io.grpc.BindableService {
        public void invoke(DirectInvocationRequest request, io.grpc.stub.StreamObserver<DirectInvocationResponse> responseObserver) {
            io.grpc.stub.ServerCalls.asyncUnimplementedUnaryCall(getInvokeMethod(), responseObserver);
        }

        public void invokeStream(DirectInvocationRequest request, io.grpc.stub.StreamObserver<DirectStreamItem> responseObserver) {
            io.grpc.stub.ServerCalls.asyncUnimplementedUnaryCall(getInvokeStreamMethod(), responseObserver);
        }

        public void health(HealthRequest request, io.grpc.stub.StreamObserver<HealthResponse> responseObserver) {
            io.grpc.stub.ServerCalls.asyncUnimplementedUnaryCall(getHealthMethod(), responseObserver);
        }

        @Override
        public final io.grpc.ServerServiceDefinition bindService() {
            return io.grpc.ServerServiceDefinition.builder(SERVICE_NAME)
                .addMethod(getInvokeMethod(),
                    io.grpc.stub.ServerCalls.asyncUnaryCall(
                        new io.grpc.stub.ServerCalls.UnaryMethod<DirectInvocationRequest, DirectInvocationResponse>() {
                            @Override
                            public void invoke(DirectInvocationRequest req, io.grpc.stub.StreamObserver<DirectInvocationResponse> resp) {
                                DirectPluginRuntimeImplBase.this.invoke(req, resp);
                            }
                        }))
                .addMethod(getInvokeStreamMethod(),
                    io.grpc.stub.ServerCalls.asyncServerStreamingCall(
                        new io.grpc.stub.ServerCalls.ServerStreamingMethod<DirectInvocationRequest, DirectStreamItem>() {
                            @Override
                            public void invoke(DirectInvocationRequest req, io.grpc.stub.StreamObserver<DirectStreamItem> resp) {
                                DirectPluginRuntimeImplBase.this.invokeStream(req, resp);
                            }
                        }))
                .addMethod(getHealthMethod(),
                    io.grpc.stub.ServerCalls.asyncUnaryCall(
                        new io.grpc.stub.ServerCalls.UnaryMethod<HealthRequest, HealthResponse>() {
                            @Override
                            public void invoke(HealthRequest req, io.grpc.stub.StreamObserver<HealthResponse> resp) {
                                DirectPluginRuntimeImplBase.this.health(req, resp);
                            }
                        }))
                .build();
        }
    }
}
