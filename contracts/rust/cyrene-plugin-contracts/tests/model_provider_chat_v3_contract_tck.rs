// ╔══════════════════════════════════════════════════════════════════════╗
// ║ 📄 File: model_provider_chat_v3_contract_tck.rs                      ║
// ║ Module: CYRENE Plugins Official                                     ║
// ║ Role: Typed multimodal model.provider.v1 chat conformance.           ║
// ║                                                                      ║
// ║ 模块：CYRENE Plugins Official                                       ║
// ║ 职责：验证 model.provider.v1 类型化多模态 chat v3。                 ║
// ╚══════════════════════════════════════════════════════════════════════╝

use cyrene_plugin_contracts::{
    model_provider::{
        CAPABILITY_ID, CHAT_COMPLETION_RESPONSE_TYPE_URL, CHAT_COMPLETION_V3_INTERFACE_VERSION,
        CHAT_COMPLETION_V3_METHOD, CHAT_COMPLETION_V3_REQUEST_TYPE_URL,
    },
    model_provider_v1::{
        chat_content_part_v3, chat_image_content_v3, chat_message, ChatCompletionRequestV3,
        ChatContentPartV3, ChatImageContentV3, ChatMessageV3,
    },
};
use prost::Message;

#[test]
fn multimodal_chat_v3_has_separate_method_and_typed_content() {
    assert_eq!(CAPABILITY_ID, "model.provider.v1");
    assert_eq!(CHAT_COMPLETION_V3_METHOD, "chat_completion_v3");
    assert_eq!(CHAT_COMPLETION_V3_INTERFACE_VERSION, "3");
    assert_eq!(
        CHAT_COMPLETION_V3_REQUEST_TYPE_URL,
        "type.cyrene.io/cyrene.model.provider.v1.ChatCompletionRequestV3"
    );
    assert_eq!(
        CHAT_COMPLETION_RESPONSE_TYPE_URL,
        "type.cyrene.io/cyrene.model.provider.v1.ChatCompletionResponse"
    );

    let request = ChatCompletionRequestV3 {
        messages: vec![ChatMessageV3 {
            role: chat_message::Role::User as i32,
            content: vec![
                ChatContentPartV3 {
                    part: Some(chat_content_part_v3::Part::Text("describe".to_string())),
                },
                ChatContentPartV3 {
                    part: Some(chat_content_part_v3::Part::Image(ChatImageContentV3 {
                        mime_type: "image/png".to_string(),
                        source: Some(chat_image_content_v3::Source::ArtifactId(
                            "asset-9".to_string(),
                        )),
                    })),
                },
            ],
            name: None,
            tool_call_id: None,
            tool_calls: vec![],
        }],
        model: Some("vision-model".to_string()),
        stream: false,
        temperature: None,
        max_tokens: None,
        tools: vec![],
        tool_choice: None,
        parallel_tool_calls: None,
        include_usage: None,
    };
    assert_eq!(
        ChatCompletionRequestV3::decode(request.encode_to_vec().as_slice()).unwrap(),
        request
    );
}
