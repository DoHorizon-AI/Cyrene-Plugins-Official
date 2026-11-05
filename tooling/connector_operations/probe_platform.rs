// Appended to a scratch copy of Platform's connector_grant.rs by manifest.py. Never committed there.
// Calls the real grant and role functions, so a changed rule shows up as a changed result.

#[cfg(test)]
mod connector_operations_probe {
    use std::collections::BTreeSet;

    use async_trait::async_trait;
    use cy_proto::workspace_v1::{UserIdentityRef, WorkspaceConnectionDescriptor};

    use super::*;
    use crate::{
        authority::service::require_directory_connector_operation_role,
        directory::{WorkspaceDirectory, WorkspaceDirectoryError},
    };

    struct ProbeDirectory {
        roles: BTreeSet<String>,
    }

    #[async_trait]
    impl WorkspaceDirectory for ProbeDirectory {
        async fn discover(
            &self,
            _user: &UserIdentityRef,
            _organization_id: &str,
            _now_unix_ms: u64,
        ) -> Result<Vec<WorkspaceConnectionDescriptor>, WorkspaceDirectoryError> {
            Ok(Vec::new())
        }

        async fn is_member(
            &self,
            _user: &UserIdentityRef,
            _organization_id: &str,
            _workspace_id: &str,
        ) -> Result<bool, WorkspaceDirectoryError> {
            Ok(true)
        }

        async fn roles_for_member(
            &self,
            _user: &UserIdentityRef,
            _organization_id: &str,
            _workspace_id: &str,
        ) -> Result<Option<BTreeSet<String>>, WorkspaceDirectoryError> {
            Ok(Some(self.roles.clone()))
        }
    }

    async fn allowed(role: Option<&str>, provider: &str, operation: &str) -> bool {
        let directory = ProbeDirectory {
            roles: role.map(str::to_owned).into_iter().collect(),
        };
        let identity = UserIdentityRef {
            issuer: "https://issuer.invalid/".to_owned(),
            subject: "probe".to_owned(),
        };
        require_directory_connector_operation_role(
            &directory, &identity, "org", "workspace", provider, operation,
        )
        .await
        .is_ok()
    }

    fn text_list(name: &str) -> Vec<String> {
        serde_json::from_str(&std::env::var(name).expect(name)).expect(name)
    }

    #[tokio::test]
    async fn dump() {
        let providers = text_list("CONNECTOR_PROBE_PROVIDERS");
        let mut operations: Vec<String> = SUPPORTED_CONNECTOR_OPERATION_IDS
            .iter()
            .map(|value| (*value).to_owned())
            .collect();
        operations.extend(text_list("CONNECTOR_PROBE_EXTRA_OPERATIONS"));
        let mut pairs = Vec::new();
        for provider in &providers {
            for operation in &operations {
                pairs.push(serde_json::json!({
                    "provider": provider,
                    "operation": operation,
                    "supported": is_supported_connector_operation(provider, operation),
                    "viewerCatalog": is_automation_viewer_connector_operation(provider, operation),
                    "writerCatalog": is_office_task_writer_connector_operation(provider, operation),
                    "transportCatalog": is_automation_transport_connector_operation(provider, operation),
                    "allowed": {
                        "operator": allowed(Some(NAVIGATOR_CONNECTOR_OPERATOR_ROLE), provider, operation).await,
                        "automationViewer": allowed(Some(NAVIGATOR_AUTOMATION_VIEWER_ROLE), provider, operation).await,
                        "officeTaskWriter": allowed(Some(NAVIGATOR_OFFICE_TASK_WRITER_ROLE), provider, operation).await,
                        "noRole": allowed(None, provider, operation).await,
                    },
                }));
            }
        }
        let dump = serde_json::json!({
            "supported": SUPPORTED_CONNECTOR_OPERATION_IDS.to_vec(),
            "roles": {
                "operator": NAVIGATOR_CONNECTOR_OPERATOR_ROLE,
                "automationViewer": NAVIGATOR_AUTOMATION_VIEWER_ROLE,
                "officeTaskWriter": NAVIGATOR_OFFICE_TASK_WRITER_ROLE,
            },
            "pairs": pairs,
        });
        std::fs::write(
            std::env::var("CONNECTOR_PROBE_OUTPUT").expect("CONNECTOR_PROBE_OUTPUT"),
            serde_json::to_vec(&dump).expect("dump is JSON"),
        )
        .expect("dump is writable");
    }
}
