// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 env_filter.rs                                                   │
// │  Package: cyrene-computer-runtime::security                         │
// │  Role: Environment variable sanitization and isolation (T75, T80).  │
// │                                                                     │
// │  模块职责：环境变量严格脱敏与过滤，防止主机敏感凭据泄露至受管命令       │
// └─────────────────────────────────────────────────────────────────────┘

use std::collections::{HashMap, HashSet};

pub struct EnvironmentFilter {
    allowed_host_vars: HashSet<&'static str>,
    sensitive_patterns: Vec<&'static str>,
}

impl Default for EnvironmentFilter {
    fn default() -> Self {
        Self::new()
    }
}

impl EnvironmentFilter {
    pub fn new() -> Self {
        let mut allowed = HashSet::new();
        allowed.insert("PATH");
        allowed.insert("LANG");
        allowed.insert("LC_ALL");
        allowed.insert("TERM");
        allowed.insert("HOME");
        allowed.insert("USER");
        allowed.insert("TMPDIR");
        allowed.insert("TZ");

        let sensitive = vec![
            "API_KEY",
            "TOKEN",
            "SECRET",
            "PASSWORD",
            "PASSWD",
            "CREDENTIAL",
            "PRIVATE",
            "AUTH",
            "COOKIE",
            "BEARER",
            "SIGNATURE",
            "SESSION",
        ];

        Self {
            allowed_host_vars: allowed,
            sensitive_patterns: sensitive,
        }
    }

    /// Filters and produces an isolated sanitized environment map.
    /// 1. Host environment variables are stripped by default, retaining only safe whitelist items.
    /// 2. User-provided environment variables are inspected: any containing sensitive patterns are rejected or stripped.
    pub fn sanitize_environment(
        &self,
        requested_env: &HashMap<String, String>,
    ) -> HashMap<String, String> {
        let mut clean_env = HashMap::new();

        // 1. Inherit safe whitelist from host
        for key in &self.allowed_host_vars {
            if let Ok(val) = std::env::var(key) {
                clean_env.insert((*key).to_string(), val);
            }
        }

        // 2. Merge requested environment, filtering out any sensitive names
        for (k, v) in requested_env {
            let upper = k.to_uppercase();
            let is_sensitive = self
                .sensitive_patterns
                .iter()
                .any(|pattern| upper.contains(pattern));

            if !is_sensitive {
                clean_env.insert(k.clone(), v.clone());
            }
        }

        clean_env
    }
}
