// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 tool_catalog.rs                                                 │
// │  Package: cyrene-agent-runtime::adapter                             │
// │  Role: Per-run tool catalog snapshot and tool.provider.v1 routing.  │
// │                                                                     │
// │  模块职责：运行开始时取得一次 ToolCatalogSnapshot；对模型做          │
// │  deterministic 平面名称投影并保留 inverse map（决策 A），当前 run    │
// │  内工具集合不变化（决策 B）。                                        │
// └─────────────────────────────────────────────────────────────────────┘

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

use async_trait::async_trait;
use serde_json::{json, Value};

use cyrene_plugin_contracts::agent_runtime_v1::{
    AgentErrorCode, AgentRunError, ToolCall, ToolCallResult, ToolDeclaration,
};
use cyrene_plugin_contracts::tool_provider_v1::{
    call_tool_response, list_tools_response, tool_content_part, CallToolRequest, CallToolResponse,
    ListToolsResponse, ToolCallOutcome, ToolCatalog,
};

use crate::adapter::tool_provider::ToolProvider;

const MAX_FLAT_TOOL_NAME: usize = 64;

/// Transport-neutral source of a `tool.provider.v1` catalog and calls.
///
/// The host (for example the plugin server package) implements this over the
/// selected plugin endpoint or provider; the runtime never performs endpoint
/// resolution itself.
#[async_trait]
pub trait ToolCatalogSource: Send + Sync {
    /// Fetch the current catalog for one binding, or for every configured binding.
    async fn list_tools(&self, binding_id: Option<&str>) -> ListToolsResponse;

    /// Execute one tool call against the canonical `(binding_id, provider_tool_id)` identity.
    async fn call_tool(&self, request: &CallToolRequest) -> CallToolResponse;
}

/// One resolved route behind a projected flat tool name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ToolRoute {
    pub binding_id: String,
    pub provider_tool_id: String,
}

/// One immutable tool catalog snapshot taken at the start of an agent run.
#[derive(Clone, Debug)]
pub struct ToolCatalogSnapshot {
    catalog_version: String,
    declarations: Vec<ToolDeclaration>,
    routes: HashMap<String, ToolRoute>,
}

impl ToolCatalogSnapshot {
    /// Project one contract catalog into a deterministic snapshot.
    pub fn from_catalog(catalog: &ToolCatalog) -> Self {
        let mut descriptors: Vec<_> = catalog.tools.iter().collect();
        descriptors.sort_by(|left, right| {
            (&left.binding_id, &left.provider_tool_id)
                .cmp(&(&right.binding_id, &right.provider_tool_id))
        });

        let mut base_counts: BTreeMap<String, usize> = BTreeMap::new();
        for descriptor in &descriptors {
            *base_counts
                .entry(sanitize(&descriptor.provider_tool_id))
                .or_insert(0) += 1;
        }

        let mut used: HashSet<String> = HashSet::new();
        let mut declarations = Vec::with_capacity(descriptors.len());
        let mut routes = HashMap::with_capacity(descriptors.len());
        for descriptor in descriptors {
            let base = sanitize(&descriptor.provider_tool_id);
            let root = if base_counts.get(&base).copied().unwrap_or(0) > 1 {
                clamp_name(format!("{}__{}", sanitize(&descriptor.binding_id), base))
            } else {
                base
            };
            let mut flat_name = root.clone();
            let mut suffix = 1usize;
            while used.contains(&flat_name) {
                flat_name = clamp_name(format!("{root}__{suffix}"));
                suffix += 1;
            }
            used.insert(flat_name.clone());
            routes.insert(
                flat_name.clone(),
                ToolRoute {
                    binding_id: descriptor.binding_id.clone(),
                    provider_tool_id: descriptor.provider_tool_id.clone(),
                },
            );
            declarations.push(ToolDeclaration {
                name: flat_name,
                description: descriptor.description.clone(),
                parameters_json_schema: if descriptor.input_schema_json.is_empty() {
                    "{}".to_string()
                } else {
                    descriptor.input_schema_json.clone()
                },
            });
        }

        Self {
            catalog_version: catalog.catalog_version.clone(),
            declarations,
            routes,
        }
    }

    /// Provider-reported opaque snapshot version.
    pub fn catalog_version(&self) -> &str {
        &self.catalog_version
    }

    /// Agent-facing declarations in deterministic order.
    pub fn declarations(&self) -> &[ToolDeclaration] {
        &self.declarations
    }

    /// Number of tools in this snapshot.
    pub fn tool_count(&self) -> usize {
        self.declarations.len()
    }

    /// Resolve one projected flat name back to its canonical identity.
    pub fn route(&self, flat_name: &str) -> Option<&ToolRoute> {
        self.routes.get(flat_name)
    }
}

/// Take one catalog snapshot from a source at the start of an agent run.
pub async fn snapshot_tool_catalog(
    source: &(dyn ToolCatalogSource + 'static),
    binding_id: Option<&str>,
) -> Result<ToolCatalogSnapshot, AgentRunError> {
    match source.list_tools(binding_id).await.result {
        Some(list_tools_response::Result::Catalog(catalog)) => {
            Ok(ToolCatalogSnapshot::from_catalog(&catalog))
        }
        Some(list_tools_response::Result::Error(error)) => Err(AgentRunError {
            code: AgentErrorCode::ToolExecutionFailed as i32,
            message: format!(
                "tool catalog snapshot failed: {} (code {})",
                error.message, error.code
            ),
            retryable: error.retryable,
            domain_details: "tools.catalog_error".to_string(),
        }),
        None => Err(AgentRunError {
            code: AgentErrorCode::ToolExecutionFailed as i32,
            message: "tool catalog snapshot carried no payload".to_string(),
            retryable: false,
            domain_details: "tools.catalog_empty_payload".to_string(),
        }),
    }
}

/// A `ToolProvider` that routes tool calls through one run's catalog snapshot.
///
/// A flat name outside the snapshot fails closed with a typed error and never
/// reaches the source; the snapshot itself cannot change during the run.
pub struct SnapshotToolProvider {
    snapshot: ToolCatalogSnapshot,
    source: Arc<dyn ToolCatalogSource>,
}

impl SnapshotToolProvider {
    /// Build a provider for one run snapshot.
    pub fn new(snapshot: ToolCatalogSnapshot, source: Arc<dyn ToolCatalogSource>) -> Self {
        Self { snapshot, source }
    }

    /// The snapshot this provider routes through.
    pub fn snapshot(&self) -> &ToolCatalogSnapshot {
        &self.snapshot
    }
}

#[async_trait]
impl ToolProvider for SnapshotToolProvider {
    async fn execute(&self, call: &ToolCall) -> Result<ToolCallResult, AgentRunError> {
        let Some(route) = self.snapshot.route(&call.tool_name) else {
            return Err(AgentRunError {
                code: AgentErrorCode::ToolExecutionFailed as i32,
                message: format!(
                    "tool '{}' is not part of this run's catalog snapshot",
                    call.tool_name
                ),
                retryable: false,
                domain_details: "tools.not_in_snapshot".to_string(),
            });
        };

        let request = CallToolRequest {
            binding_id: route.binding_id.clone(),
            provider_tool_id: route.provider_tool_id.clone(),
            arguments_json: call.arguments_json.clone(),
            catalog_version: Some(self.snapshot.catalog_version().to_string()),
        };

        match self.source.call_tool(&request).await.result {
            Some(call_tool_response::Result::Outcome(outcome)) => Ok(ToolCallResult {
                call_id: call.call_id.clone(),
                tool_name: call.tool_name.clone(),
                output_json: render_outcome(&outcome),
                is_error: outcome.is_error,
            }),
            Some(call_tool_response::Result::Error(error)) => Err(AgentRunError {
                code: AgentErrorCode::ToolExecutionFailed as i32,
                message: format!(
                    "tool '{}' failed: {} (code {})",
                    call.tool_name, error.message, error.code
                ),
                retryable: error.retryable,
                domain_details: format!("tools.provider_error.{}", error.code),
            }),
            None => Err(AgentRunError {
                code: AgentErrorCode::ToolExecutionFailed as i32,
                message: format!("tool '{}' returned no payload", call.tool_name),
                retryable: false,
                domain_details: "tools.empty_payload".to_string(),
            }),
        }
    }
}

/// Render tool content for the model: a lone text part stays verbatim,
/// everything else becomes a compact JSON part list.
fn render_outcome(outcome: &ToolCallOutcome) -> String {
    if outcome.content.len() == 1 {
        if let Some(tool_content_part::Content::Text(text)) = &outcome.content[0].content {
            return text.text.clone();
        }
    }
    let parts: Vec<Value> = outcome
        .content
        .iter()
        .map(|part| match &part.content {
            Some(tool_content_part::Content::Text(text)) => {
                json!({ "type": "text", "text": text.text })
            }
            Some(tool_content_part::Content::Json(content)) => {
                serde_json::from_str::<Value>(&content.json)
                    .unwrap_or_else(|_| json!({ "type": "json", "json": content.json }))
            }
            None => json!({ "type": "empty" }),
        })
        .collect();
    serde_json::to_string(&parts).unwrap_or_else(|_| "[]".to_string())
}

/// Map a provider tool id to a model-safe flat name.
///
/// The model-facing alphabet is `[A-Za-z0-9_-]` (the intersection of the
/// supported vendor tool-name grammars), so dots, slashes, and spaces all
/// fold to `_`.
fn sanitize(name: &str) -> String {
    let mapped: String = name
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '_' | '-') {
                character
            } else {
                '_'
            }
        })
        .collect();
    let mapped = if mapped.is_empty() {
        "tool".to_string()
    } else {
        mapped
    };
    clamp_name(mapped)
}

fn clamp_name(name: String) -> String {
    if name.len() <= MAX_FLAT_TOOL_NAME {
        return name;
    }
    name.chars().take(MAX_FLAT_TOOL_NAME).collect()
}
