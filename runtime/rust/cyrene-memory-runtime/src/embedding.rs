// SPDX-License-Identifier: Apache-2.0
//! Contract-governed Embedding Client Integration (Task T69)
//!
//! Enforces: Obtain embeddings only through model.provider.v1/embeddings.
//! Memory provider does not own vendor credentials or implement local neural models.

use async_trait::async_trait;

#[derive(Debug, Clone)]
pub struct EmbeddingRequest {
    pub model: String,
    pub input_texts: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct EmbeddingResponse {
    pub embeddings: Vec<Vec<f32>>,
    pub dimension: usize,
}

#[async_trait]
pub trait ModelEmbeddingClient: Send + Sync {
    /// Calls model.provider.v1/embeddings to generate dense vector embeddings.
    async fn generate_embeddings(
        &self,
        request: EmbeddingRequest,
    ) -> Result<EmbeddingResponse, String>;
}

/// Mock client used for testing contract integration without vendor credentials.
pub struct ContractModelEmbeddingClient {
    expected_dimension: usize,
}

impl ContractModelEmbeddingClient {
    pub fn new(expected_dimension: usize) -> Self {
        Self { expected_dimension }
    }
}

#[async_trait]
impl ModelEmbeddingClient for ContractModelEmbeddingClient {
    async fn generate_embeddings(
        &self,
        request: EmbeddingRequest,
    ) -> Result<EmbeddingResponse, String> {
        let mut embeddings = Vec::with_capacity(request.input_texts.len());
        for text in &request.input_texts {
            // Deterministic synthetic vector based on text hash for test reproducibility
            let mut vec = vec![0.0f32; self.expected_dimension];
            let hash = text.bytes().fold(0u32, |acc, b| acc.wrapping_add(b as u32));
            for (i, val) in vec.iter_mut().enumerate().take(self.expected_dimension) {
                *val = ((hash.wrapping_add(i as u32) % 100) as f32) / 100.0;
            }
            // Normalize
            let norm: f32 = vec.iter().map(|x| x * x).sum::<f32>().sqrt();
            if norm > 0.0 {
                for x in &mut vec {
                    *x /= norm;
                }
            }
            embeddings.push(vec);
        }
        Ok(EmbeddingResponse {
            embeddings,
            dimension: self.expected_dimension,
        })
    }
}
