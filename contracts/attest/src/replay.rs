use crate::{lower_hex, ReplayEvent};
use sha2::{Digest, Sha256, Sha384};

pub const MAX_VERIFICATION_BYTES: usize = 65_536;
pub const MAX_COMPOSE_BYTES: usize = 65_536;
pub const MAX_EVENTS: usize = 64;
pub const MAX_EVENT_BYTES: usize = 32_768;

pub(crate) fn decode_hex(value: &str) -> Result<Vec<u8>, &'static str> {
    if value.len() % 2 != 0
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("invalid lowercase hex");
    }
    hex::decode(value).map_err(|_| "invalid lowercase hex")
}

pub(crate) fn validate_evidence(
    app_compose: &str,
    events: &[ReplayEvent],
) -> Result<(), &'static str> {
    if app_compose.len() > MAX_COMPOSE_BYTES || events.len() > MAX_EVENTS {
        return Err("evidence exceeds bounds");
    }
    let mut total = 0;
    for event in events {
        if event.event.len() > 128 || event.event_payload.len() > 8192 {
            return Err("event exceeds bounds");
        }
        lower_hex::<48>(&event.digest)?;
        decode_hex(&event.event_payload)?;
        total += event.digest.len() + event.event.len() + event.event_payload.len();
        if total > MAX_EVENT_BYTES {
            return Err("event log exceeds bounds");
        }
    }
    Ok(())
}

pub fn replay_rtmr3(
    app_compose: &str,
    events: &[ReplayEvent],
    expected_rtmr3: &[u8; 48],
) -> Result<([u8; 48], [u8; 32]), &'static str> {
    validate_evidence(app_compose, events)?;
    let compose_hash: [u8; 32] = Sha256::digest(app_compose.as_bytes()).into();
    let mut compose_seen = false;
    let mut key_provider = None;
    let mut rtmr3 = [0; 48];
    for event in events.iter().filter(|event| event.imr == 3) {
        if event.event_type != 134_217_729 {
            return Err("unsupported RTMR3 event type");
        }
        let payload = decode_hex(&event.event_payload)?;
        let digest = lower_hex::<48>(&event.digest)?;
        let mut hash = Sha384::new();
        hash.update(event.event_type.to_le_bytes());
        hash.update(b":");
        hash.update(event.event.as_bytes());
        hash.update(b":");
        hash.update(&payload);
        if hash.finalize()[..] != digest {
            return Err("event digest mismatch");
        }
        match event.event.as_str() {
            "compose-hash" => {
                if compose_seen || payload != compose_hash {
                    return Err("compose hash must match exactly once");
                }
                compose_seen = true;
            }
            "key-provider" => {
                if key_provider.replace(digest).is_some() {
                    return Err("key provider must occur exactly once");
                }
            }
            _ => {}
        }
        let mut hash = Sha384::new();
        hash.update(rtmr3);
        hash.update(digest);
        rtmr3 = hash.finalize().into();
    }
    if rtmr3 != *expected_rtmr3 {
        return Err("RTMR3 mismatch");
    }
    if !compose_seen {
        return Err("missing compose hash event");
    }
    Ok((
        key_provider.ok_or("missing key provider event")?,
        compose_hash,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn extend(events: &[ReplayEvent]) -> [u8; 48] {
        let mut register = [0; 48];
        for event in events.iter().filter(|event| event.imr == 3) {
            let mut bytes = register.to_vec();
            bytes.extend(hex::decode(&event.digest).unwrap());
            register = Sha384::digest(bytes).into();
        }
        register
    }

    fn upstream() -> (String, Vec<ReplayEvent>, [u8; 48]) {
        let tcb: Value =
            serde_json::from_str(include_str!("../tests/vendor/upstream/tcb_info.json")).unwrap();
        (
            tcb["app_compose"].as_str().unwrap().into(),
            serde_json::from_value(tcb["event_log"].clone()).unwrap(),
            lower_hex::<48>(tcb["rtmr3"].as_str().unwrap()).unwrap(),
        )
    }

    #[test]
    fn replay_upstream_mpc_log() {
        let (compose, events, register) = upstream();
        assert_eq!(extend(&events), register);
        let (provider, compose_hash) = replay_rtmr3(&compose, &events, &register).unwrap();
        let event = events
            .iter()
            .find(|event| event.imr == 3 && event.event == "key-provider")
            .unwrap();
        assert_eq!(hex::encode(provider), event.digest);
        assert_eq!(
            compose_hash.as_slice(),
            &Sha256::digest(compose.as_bytes())[..]
        );
    }

    #[test]
    fn replay_shared_registration_vectors() {
        let vectors: Value =
            serde_json::from_str(include_str!("../../../fixtures/attest/vectors.json")).unwrap();
        for vector in vectors["registration_args"].as_array().unwrap() {
            let args = &vector["arguments"];
            let events: Vec<ReplayEvent> =
                serde_json::from_value(args["event_log"].clone()).unwrap();
            let (provider, compose_hash) = replay_rtmr3(
                args["app_compose"].as_str().unwrap(),
                &events,
                &extend(&events),
            )
            .unwrap();
            assert_eq!(hex::encode(compose_hash), events[0].event_payload);
            assert_eq!(
                hex::encode(provider),
                events
                    .iter()
                    .find(|event| event.event == "key-provider")
                    .unwrap()
                    .digest
            );
        }
    }

    #[test]
    fn replay_requires_unique_events_even_with_matching_register() {
        let (compose, events, _) = upstream();
        for name in ["compose-hash", "key-provider"] {
            let mut duplicate = events.clone();
            duplicate.push(
                events
                    .iter()
                    .find(|event| event.imr == 3 && event.event == name)
                    .unwrap()
                    .clone(),
            );
            assert!(replay_rtmr3(&compose, &duplicate, &extend(&duplicate)).is_err());
            let omitted: Vec<_> = events
                .iter()
                .filter(|event| event.event != name)
                .cloned()
                .collect();
            assert!(replay_rtmr3(&compose, &omitted, &extend(&omitted)).is_err());
        }
        assert!(replay_rtmr3(&compose, &[], &[0; 48]).is_err());
    }

    #[test]
    fn replay_refuses_digest_payload_type_compose_and_order_changes() {
        let (compose, events, register) = upstream();
        let index = events.iter().position(|event| event.imr == 3).unwrap();
        for mutation in 0..4 {
            let mut changed = events.clone();
            match mutation {
                0 => changed[index].digest = "00".repeat(48),
                1 => changed[index].event_payload.push_str("00"),
                2 => changed[index].event_type += 1,
                _ => changed.reverse(),
            }
            assert!(replay_rtmr3(&compose, &changed, &register).is_err());
        }
        assert!(replay_rtmr3(&(compose + " "), &events, &register).is_err());
    }

    #[test]
    fn evidence_bounds_and_non_rtmr3_encodings_are_checked() {
        let (_, events, _) = upstream();
        let mut event = events[0].clone();
        event.imr = 0;
        event.digest = "00".repeat(48);
        event.event_payload.clear();
        assert!(validate_evidence(&"x".repeat(MAX_COMPOSE_BYTES), &[event.clone()]).is_ok());
        assert!(validate_evidence(&"x".repeat(MAX_COMPOSE_BYTES + 1), &[]).is_err());
        assert!(validate_evidence("", &vec![event.clone(); MAX_EVENTS]).is_ok());
        assert!(validate_evidence("", &vec![event.clone(); MAX_EVENTS + 1]).is_err());
        for payload in ["a", "AA", "0x00", "00 ", "zz"] {
            event.event_payload = payload.into();
            assert!(validate_evidence("", &[event.clone()]).is_err());
        }
        event.event_payload = "00".repeat(4096);
        assert!(validate_evidence("", &[event.clone()]).is_ok());
        event.event_payload.push_str("00");
        assert!(validate_evidence("", &[event.clone()]).is_err());
        event.event_payload.clear();
        event.event = "x".repeat(129);
        assert!(validate_evidence("", &[event.clone()]).is_err());
        event.event.clear();
        event.digest = "AA".repeat(48);
        assert!(validate_evidence("", &[event]).is_err());
    }

    #[test]
    fn event_requires_a_json_object() {
        let (_, events, _) = upstream();
        let event = &events[0];
        let array = serde_json::json!([
            event.imr,
            event.event_type,
            event.digest,
            event.event,
            event.event_payload
        ]);
        assert!(serde_json::from_value::<ReplayEvent>(array).is_err());
        assert!(
            serde_json::from_value::<ReplayEvent>(serde_json::to_value(event).unwrap()).is_ok()
        );
    }
}
