// ╔══════════════════════════════════════════════════════════════════════╗
// ║ 📄 File: speech_provider_contract_tck.rs                             ║
// ║ Module: CYRENE Plugins Official                                     ║
// ║ Role: speech.provider.v1 payload conformance and identifiers.        ║
// ║                                                                      ║
// ║ 模块：CYRENE Plugins Official                                       ║
// ║ 职责：验证 speech.provider.v1 负载及稳定标识符。                    ║
// ╚══════════════════════════════════════════════════════════════════════╝

use cyrene_plugin_contracts::{
    speech_provider::{
        CAPABILITY_ID, INTERFACE_VERSION, METHOD_TRANSCRIBE, TRANSCRIBE_REQUEST_TYPE_URL,
        TRANSCRIBE_RESPONSE_TYPE_URL,
    },
    speech_provider_v1::{
        speech_input, transcribe_response, SpeechInput, TranscribeRequest, TranscribeResponse,
        TranscriptionResult, TranscriptionStatus,
    },
};
use prost::Message;

#[test]
fn speech_capability_and_type_urls_are_stable() {
    assert_eq!(CAPABILITY_ID, "speech.provider.v1");
    assert_eq!(INTERFACE_VERSION, "1");
    assert_eq!(METHOD_TRANSCRIBE, "transcribe");
    assert_eq!(
        TRANSCRIBE_REQUEST_TYPE_URL,
        "type.cyrene.io/cyrene.speech.provider.v1.TranscribeRequest"
    );
    assert_eq!(
        TRANSCRIBE_RESPONSE_TYPE_URL,
        "type.cyrene.io/cyrene.speech.provider.v1.TranscribeResponse"
    );
}

#[test]
fn speech_media_source_and_transcription_status_round_trip() {
    let bytes_request = TranscribeRequest {
        input: Some(SpeechInput {
            mime_type: "audio/wav".to_string(),
            source: Some(speech_input::Source::Data(b"wave-bytes".to_vec())),
        }),
        language_hint: Some("en".to_string()),
        request_id: Some("request-7".to_string()),
    };
    assert_eq!(
        TranscribeRequest::decode(bytes_request.encode_to_vec().as_slice()).unwrap(),
        bytes_request
    );

    let artifact_request = TranscribeRequest {
        input: Some(SpeechInput {
            mime_type: "audio/ogg".to_string(),
            source: Some(speech_input::Source::ArtifactId(
                "media-asset-9".to_string(),
            )),
        }),
        language_hint: None,
        request_id: None,
    };
    assert_eq!(
        TranscribeRequest::decode(artifact_request.encode_to_vec().as_slice()).unwrap(),
        artifact_request
    );

    let response = TranscribeResponse {
        result: Some(transcribe_response::Result::Transcription(
            TranscriptionResult {
                status: TranscriptionStatus::NoSpeech as i32,
                text: String::new(),
                detected_language: None,
                duration_ms: Some(1250),
                model: Some("sensevoice-local".to_string()),
                segments: vec![],
            },
        )),
    };
    assert_eq!(
        TranscribeResponse::decode(response.encode_to_vec().as_slice()).unwrap(),
        response
    );
}
