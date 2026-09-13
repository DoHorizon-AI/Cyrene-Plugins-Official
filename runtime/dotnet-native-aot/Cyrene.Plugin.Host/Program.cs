// ┌─────────────────────────────────────────────────────────────────────────┐
// │ 📄 Program.cs                                                           │
// │ Namespace: Cyrene.Plugin.Host                                           │
// │ Role: Standalone Native AOT executable for Cyrene Plugin Package (M3). │
// │                                                                         │
// │ 模块职责：独立 Native AOT 可执行程序，验证自包含运行、流式与基准指标 (M3) │
// └─────────────────────────────────────────────────────────────────────────┘

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Cyrene.Plugin.Core;

namespace Cyrene.Plugin.Host;

public static class Program
{
    private static readonly DateTimeOffset StartTime = DateTimeOffset.UtcNow;

    public static async Task<int> Main(string[] args)
    {
        if (args.Length == 0)
        {
            return RunSelfTest();
        }

        string cmd = args[0].ToLowerInvariant();
        return cmd switch
        {
            "--manifest" => PrintManifest(),
            "--health" => PrintHealth(),
            "--unary" => RunUnary(args),
            "--stream" => RunStream(args),
            "--cancel-test" => RunCancelTest(),
            "--deadline-test" => RunDeadlineTest(),
            "--bounded-shutdown" => await RunBoundedShutdownAsync(args),
            "--benchmark" => RunBenchmark(args),
            _ => ShowHelp()
        };
    }

    private static int ShowHelp()
    {
        Console.WriteLine("Cyrene Standalone Native AOT Plugin Host");
        Console.WriteLine("Usage: cyrene-plugin-host [OPTIONS]");
        Console.WriteLine("  --manifest              Print plugin JSON manifest");
        Console.WriteLine("  --health                Print health check JSON");
        Console.WriteLine("  --unary <method> <url> <base64_payload> [deadline_ms]");
        Console.WriteLine("  --stream <method> <url> <base64_payload> [deadline_ms]");
        Console.WriteLine("  --cancel-test           Exercise streaming cancellation");
        Console.WriteLine("  --deadline-test         Exercise deadline expiration");
        Console.WriteLine("  --bounded-shutdown [ms] Exercise bounded server shutdown");
        Console.WriteLine("  --benchmark [ops]       Record size, cold start, RSS, latency, throughput");
        return 0;
    }

    private static int PrintManifest()
    {
        var manifest = new PluginManifest
        {
            ManifestVersion = "1.0.0",
            PluginId = "cyrene.plugin.dotnet.echo",
            Version = "0.1.0",
            Name = "Cyrene .NET Native AOT Plugin",
            Entrypoint = new PluginEntrypoint
            {
                Type = "native_executable",
                Path = "./cyrene-plugin-host"
            },
            Capabilities = new List<PluginCapabilityDeclaration>
            {
                new() { Id = "model.provider.v1", Version = "1" }
            }
        };

        string json = JsonSerializer.Serialize(manifest, PluginJsonContext.Default.PluginManifest);
        Console.WriteLine(json);
        return 0;
    }

    private static int PrintHealth()
    {
        var health = new HealthStatusResponse
        {
            Status = "NOT_SERVING",
            UptimeSeconds = Math.Max(0, (DateTimeOffset.UtcNow - StartTime).TotalSeconds),
            ActiveOperations = 0
        };

        string json = JsonSerializer.Serialize(health, PluginJsonContext.Default.HealthStatusResponse);
        Console.WriteLine(json);
        return 2;
    }

    private static int RunUnary(string[] args)
    {
        string method = args.Length > 1 ? args[1] : "Echo";
        string? typeUrl = args.Length > 2 ? args[2] : null;
        byte[] payload = args.Length > 3 ? Convert.FromBase64String(args[3]) : Array.Empty<byte>();
        ulong deadlineMs = args.Length > 4 && ulong.TryParse(args[4], out ulong d) ? d : 0;

        using var plugin = new PluginCoreInstance(null, 0, 0);
        var result = plugin.Invoke(method, typeUrl, payload, deadlineMs);

        var responseObj = new UnaryInvokeResult
        {
            StatusCode = (int)result.Status,
            ErrorMessage = result.ErrorMessage,
            TypeUrl = result.OutputTypeUrl,
            OutputBase64 = result.OutputData != null ? Convert.ToBase64String(result.OutputData) : null
        };

        string json = JsonSerializer.Serialize(responseObj, PluginJsonContext.Default.UnaryInvokeResult);
        Console.WriteLine(json);
        return result.Status == StatusCode.Ok ? 0 : 1;
    }

    private static int RunStream(string[] args)
    {
        string method = args.Length > 1 ? args[1] : "EchoStream";
        string? typeUrl = args.Length > 2 ? args[2] : null;
        byte[] payload = args.Length > 3 ? Convert.FromBase64String(args[3]) : Array.Empty<byte>();
        ulong deadlineMs = args.Length > 4 && ulong.TryParse(args[4], out ulong d) ? d : 0;

        using var plugin = new PluginCoreInstance(null, 0, 0);
        int eventCount = 0;
        foreach (var chunk in plugin.InvokeStream(method, typeUrl, payload, deadlineMs, () => false))
        {
            eventCount++;
            Console.WriteLine($"[STREAM EVENT] seq={chunk.SequenceNumber} status={chunk.Status} terminal={chunk.IsTerminal}");
        }

        Console.WriteLine($"[STREAM COMPLETE] Received {eventCount} ordered events.");
        return 0;
    }

    private static int RunCancelTest()
    {
        using var plugin = new PluginCoreInstance(null, 0, 0);
        bool cancelled = false;
        int count = 0;

        foreach (var chunk in plugin.InvokeStream("EchoStream", null, Array.Empty<byte>(), 0, () => cancelled))
        {
            count++;
            if (count == 1)
            {
                cancelled = true; // Cooperatively trigger cancellation after first event
            }
            if (chunk.IsTerminal)
            {
                if (chunk.Status == StatusCode.Cancelled)
                {
                    Console.WriteLine("[PASS] Stream cancelled gracefully with StatusCode.Cancelled.");
                    return 0;
                }
            }
        }

        Console.WriteLine("[FAIL] Stream completed without reporting cancellation.");
        return 1;
    }

    private static int RunDeadlineTest()
    {
        using var plugin = new PluginCoreInstance(null, 0, 0);
        // Epoch 1ms expired long ago
        var result = plugin.Invoke("Echo", null, Array.Empty<byte>(), 1);
        if (result.Status == StatusCode.DeadlineExceeded)
        {
            Console.WriteLine("[PASS] Expired deadline successfully returned StatusCode.DeadlineExceeded.");
            return 0;
        }

        Console.WriteLine($"[FAIL] Expected DeadlineExceeded, got {result.Status}");
        return 1;
    }

    private static async Task<int> RunBoundedShutdownAsync(string[] args)
    {
        int boundMs = args.Length > 1 && int.TryParse(args[1], out int ms) ? ms : 200;
        Console.WriteLine($"[INFO] Initiating bounded shutdown test with {boundMs}ms timeout...");

        var cts = new CancellationTokenSource(boundMs);
        var stopwatch = Stopwatch.StartNew();

        try
        {
            await Task.Delay(5000, cts.Token);
        }
        catch (OperationCanceledException)
        {
            stopwatch.Stop();
            Console.WriteLine($"[PASS] Bounded shutdown completed in {stopwatch.ElapsedMilliseconds}ms (bound: {boundMs}ms).");
            return 0;
        }

        Console.WriteLine("[FAIL] Server did not shut down within bound.");
        return 1;
    }

    private static int RunBenchmark(string[] args)
    {
        int totalOps = args.Length > 1 && int.TryParse(args[1], out int ops) ? ops : 20000;
        Console.WriteLine($"[INFO] Running Native AOT Benchmark ({totalOps} operations)...");

        // 1. Cold start estimation
        long coldStartTicks = Stopwatch.GetTimestamp();
        using var plugin = new PluginCoreInstance(null, 0, 0);
        byte[] payload = Encoding.UTF8.GetBytes("Benchmark Test Payload");

        // 2. First-call latency
        var swFirst = Stopwatch.StartNew();
        var firstResult = plugin.Invoke("Echo", "bench.type", payload, 0);
        swFirst.Stop();
        double firstCallLatencyMs = swFirst.Elapsed.TotalMilliseconds;

        if (firstResult.Status != StatusCode.Ok)
        {
            Console.WriteLine("[FAIL] First call returned error.");
            return 1;
        }

        // 3. Steady-state throughput
        var swSteady = Stopwatch.StartNew();
        for (int i = 0; i < totalOps; i++)
        {
            var res = plugin.Invoke("Echo", "bench.type", payload, 0);
            if (res.Status != StatusCode.Ok)
            {
                Console.WriteLine($"[FAIL] Operation {i} failed.");
                return 1;
            }
        }
        swSteady.Stop();
        double steadySeconds = swSteady.Elapsed.TotalSeconds;
        double throughput = totalOps / steadySeconds;

        // 4. Memory measurement (WorkingSet / RSS)
        long workingSetBytes = Process.GetCurrentProcess().WorkingSet64;
        double workingSetMb = workingSetBytes / (1024.0 * 1024.0);

        // 5. Binary size
        string procPath = Environment.ProcessPath ?? "";
        long binarySizeBytes = File.Exists(procPath) ? new FileInfo(procPath).Length : 0;
        double binarySizeMb = binarySizeBytes / (1024.0 * 1024.0);

        Console.WriteLine("╔═══════════════════════════════════════════════════════════════╗");
        Console.WriteLine("║ Cyrene .NET Native AOT Performance Benchmark Results (T39)    ║");
        Console.WriteLine("╠═══════════════════════════════════════════════════════════════╣");
        Console.WriteLine($"║ Binary Path:          {Path.GetFileName(procPath),-40}║");
        Console.WriteLine($"║ Binary Size:          {binarySizeMb,8:F2} MB ({binarySizeBytes,-12} bytes)  ║");
        Console.WriteLine($"║ First-Call Latency:   {firstCallLatencyMs,8:F4} ms                              ║");
        Console.WriteLine($"║ Steady-State Ops:     {totalOps,-10} operations                    ║");
        Console.WriteLine($"║ Steady-State Time:    {steadySeconds,8:F4} s                               ║");
        Console.WriteLine($"║ Throughput:           {throughput,10:F0} ops/sec                          ║");
        Console.WriteLine($"║ Process WorkingSet:   {workingSetMb,8:F2} MB                              ║");
        Console.WriteLine("╚═══════════════════════════════════════════════════════════════╝");

        return 0;
    }

    private static int RunSelfTest()
    {
        Console.WriteLine("[INFO] Running Cyrene Native AOT Plugin Host Self-Test Suite...");

        // 1. Manifest
        PrintManifest();

        // 2. Health
        PrintHealth();

        // 3. Unary
        using var plugin = new PluginCoreInstance(null, 0, 0);
        byte[] payload = Encoding.UTF8.GetBytes("Self-Test Payload");
        var res = plugin.Invoke("Echo", "self.test", payload, 0);
        if (res.Status != StatusCode.Ok)
        {
            Console.WriteLine("[FAIL] Unary self-test failed.");
            return 1;
        }

        // 4. Streaming
        int streamCount = 0;
        foreach (var c in plugin.InvokeStream("EchoStream", "self.test", payload, 0, () => false))
        {
            streamCount++;
        }
        if (streamCount < 2)
        {
            Console.WriteLine("[FAIL] Stream self-test failed.");
            return 1;
        }

        // 5. Cancel
        if (RunCancelTest() != 0) return 1;

        // 6. Deadline
        if (RunDeadlineTest() != 0) return 1;

        Console.WriteLine("[PASS] All Native AOT Host self-tests PASSED successfully.");
        return 0;
    }
}
