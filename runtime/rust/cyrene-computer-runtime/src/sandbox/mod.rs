pub mod provider;

#[cfg(all(feature = "mxc", target_os = "linux"))]
mod mxc;

#[cfg(feature = "mxc")]
#[doc(hidden)]
pub mod mxc_protocol;

pub use provider::{
    BoundedManagedExecutionProvider, ComputerExecutionBackend, ExternalSandboxProviderAdapter,
    IsolationLevel,
};

#[cfg(all(feature = "mxc", target_os = "linux"))]
pub use mxc::MicrosoftMxcSandboxProviderAdapter;
