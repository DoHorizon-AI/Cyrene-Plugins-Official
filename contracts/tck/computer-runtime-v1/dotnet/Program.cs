using Cyrene.Computer.Runtime.V1;
using Google.Protobuf;
using Google.Protobuf.WellKnownTypes;

// 1. Descriptors and enum numbers are contract facts, not implementation details.
if (ListDirRequest.Descriptor.FullName != "cyrene.computer.runtime.v1.ListDirRequest")
{
    throw new InvalidOperationException("ListDirRequest descriptor moved.");
}
if (ListDirResponse.Descriptor.FullName != "cyrene.computer.runtime.v1.ListDirResponse")
{
    throw new InvalidOperationException("ListDirResponse descriptor moved.");
}
if ((int)ComputerErrorCode.PathTraversalDenied != 1
    || (int)ComputerErrorCode.ExecutionDenied != 4
    || (int)ComputerErrorCode.ArtifactNotFound != 5)
{
    throw new InvalidOperationException("ComputerErrorCode numbers are not stable.");
}

// 2. Bounded filesystem payloads round-trip through Any.
var listRequest = new ListDirRequest
{
    Path = ".",
    MaxDepth = 2,
};
var packedRequest = Any.Pack(listRequest);
if (!packedRequest.Is(ListDirRequest.Descriptor)
    || packedRequest.Unpack<ListDirRequest>().MaxDepth != 2)
{
    throw new InvalidOperationException("ListDirRequest did not round-trip.");
}

var listing = new DirEntries();
listing.Entries.Add(new DirEntry
{
    Name = "Cargo.toml",
    IsDirectory = false,
    SizeBytes = 512,
    ModifiedAtMs = 1726200000000,
});
var listResponse = new ListDirResponse { Entries = listing.Clone() };
var packedResponse = Any.Pack(listResponse);
var unpackedResponse = packedResponse.Unpack<ListDirResponse>();
if (unpackedResponse.ResultCase != ListDirResponse.ResultOneofCase.Entries
    || unpackedResponse.Entries.Entries[0].Name != "Cargo.toml"
    || unpackedResponse.Entries.Entries[0].IsDirectory)
{
    throw new InvalidOperationException("ListDirResponse did not round-trip.");
}

// 3. Errors stay typed inside the capability payload.
var denied = new ListDirResponse
{
    Error = new ComputerError
    {
        Code = ComputerErrorCode.PathTraversalDenied,
        Message = "Path '../' escapes all allowed root boundaries",
        Retryable = false,
    },
};
var unpackedDenied = Any.Pack(denied).Unpack<ListDirResponse>();
if (unpackedDenied.ResultCase != ListDirResponse.ResultOneofCase.Error
    || unpackedDenied.Error.Code != ComputerErrorCode.PathTraversalDenied
    || unpackedDenied.Error.Retryable)
{
    throw new InvalidOperationException("Typed error payload did not round-trip.");
}

// 4. Execution evidence preserves hash, truncation flags, and resource usage.
var evidence = new ExecutionEvidence
{
    ExitCode = 0,
    DurationMs = 125,
    Stdout = "Linux build-worker-01 6.18.33.2",
    StdoutTruncated = false,
    Sha256Hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ResourceUsage = new ExecutionResourceUsage
    {
        CpuTimeMs = 80,
        PeakMemoryBytes = 4194304,
    },
};
var unpackedEvidence = Any.Pack(evidence).Unpack<ExecutionEvidence>();
if (unpackedEvidence.DurationMs != 125
    || unpackedEvidence.Sha256Hash != evidence.Sha256Hash
    || unpackedEvidence.ResourceUsage.PeakMemoryBytes != 4194304)
{
    throw new InvalidOperationException("ExecutionEvidence did not round-trip.");
}

Console.WriteLine("computer.runtime.v1 generated C# payload TCK: PASS");
