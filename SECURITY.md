# Security policy

## Reporting a vulnerability

Use GitHub private vulnerability reporting for the repository. Do not open a
public issue containing exploit details, credentials, access tokens, private
endpoint references, tenant data, or model and dataset contents.

Include the affected Plugin ID and version, capability and method, deployment
mode, a minimal reproduction, and the expected security boundary. If a secret
may have been exposed, rotate or revoke it before sending the report.

## Security boundary

Plugins own replaceable capability implementations. They do not establish the
Platform process, identity, lease, fence, resource-isolation, or endpoint-grant
boundary. A Product obtains an authorized opaque `connection_ref` through the
Platform lifecycle and then calls the selected Plugin directly using the
versioned capability contract.

Plugin endpoints must validate capability, interface version, method, typed
payload, deadline, and cancellation. Configured endpoints fail closed when a
call cannot be authenticated, resolved, or completed. An explicitly disabled
optional capability may use its documented no-op behavior.

## Important non-guarantees

- A passing schema or conformance test is not proof that arbitrary Plugin code
  is safe to execute on a host.
- A compiled prototype or unconfigured provider is not a deployable or
  security-hardened runtime claim.
- Test adapters, simulated providers, and local loopback transports are not
  production authentication or tenant-isolation evidence.
- Publishing this repository does not publish credentials, deployment
  configuration, protected datasets, model weights, or private infrastructure.
