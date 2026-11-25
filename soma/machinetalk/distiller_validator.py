"""distiller_validator — reference implementation (stdlib only).

Wired into the engine where create_soma_node is handled: parse `output`, run
validate_output(...), on accept run write_stage_output(...), then derive the
verdict engine-side. Error names below are what the checkpoint/board should show.
"""
import hashlib
import json
import re

SIGNAL_TYPES = {"DIRECTIVE", "CONSTRAINT", "PREFERENCE", "QUESTION", "CORRECTION", "CONFIRMATION"}
PRECEDENCE = ["CONSTRAINT", "CORRECTION", "DIRECTIVE", "QUESTION", "PREFERENCE", "CONFIRMATION"]
PREC_RANK = {s: i for i, s in enumerate(PRECEDENCE)}
ARTIFACT_TYPES = {"HEREDOC", "METADATA_FOOTER", "CONSTRAINT_BLOCK", "VERSION_LOCK", "CODE_FENCE"}
EVIDENCE_CONF = {"EXPLICIT": 1.00, "ARTIFACT": 0.90, "KEYWORD": 0.60, "NONE": 0.00}
REQUIRED_KEYS = {"session_id", "chatset_id", "chatset_hash", "classified",
                 "artifacts", "intent_anchor", "out_of_domain", "stack_inferred"}


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def chatset_hash_for(raw_chat_set):
    return hashlib.sha256(canonical_json(raw_chat_set).encode("utf-8")).hexdigest()


def segment(text):
    parts = re.split(r"(?<=[.!?\n])", text)
    return [p.strip() for p in parts if p.strip()]


def validate_output(output_str, chatset_id, expected_hash, raw_chat_set):
    try:
        doc = json.loads(output_str)
    except json.JSONDecodeError as e:
        return False, "JSON_PARSE", f"json_parse:{e.msg}@{e.pos}", None, True
    if not isinstance(doc, dict):
        return False, "SCHEMA_FAILURE", "output is not a JSON object", None, False
    missing = sorted(REQUIRED_KEYS - set(doc))
    extra = sorted(set(doc) - REQUIRED_KEYS)
    if missing or extra:
        return False, "SCHEMA_FAILURE", f"missing:{missing} extra:{extra}", doc, False
    if doc["session_id"] is None or doc["chatset_id"] != chatset_id:
        return False, "SCHEMA_FAILURE", "chatset_id mismatch", doc, False
    h = doc["chatset_hash"]
    if not isinstance(h, str) or not h:
        return False, "HASH_MISSING", "chatset_hash absent", doc, False
    if h != expected_hash:
        return False, "HASH_MISMATCH", "chatset_hash != expected_hash", doc, False

    segs = [segment(str(t.get("content", ""))) for t in raw_chat_set]
    user_turns = {i for i, t in enumerate(raw_chat_set) if t.get("role") == "USER"}

    for e in doc["classified"]:
        t, s = e.get("turn"), e.get("seg")
        if not isinstance(t, int) or not 0 <= t < len(raw_chat_set):
            return False, "SCHEMA_FAILURE", f"bad entry turn={t}", doc, False
        raw = str(raw_chat_set[t].get("content", ""))
        txt = e.get("text")
        if not isinstance(txt, str) or txt not in raw:
            return False, "ANCHOR_MISMATCH", f"text not verbatim in turn {t}", doc, False
        # Engine derives the canonical seg: exact segment preferred, containing
        # segment accepted. The model's seg is advisory.
        exact = next((i for i, seg_text in enumerate(segs[t]) if seg_text == txt), None)
        if exact is not None:
            e["seg"] = exact
        else:
            contain = next((i for i, seg_text in enumerate(segs[t])
                            if txt in seg_text or seg_text in txt), None)
            if contain is None:
                return False, "ANCHOR_MISMATCH", f"text not mappable to a segment of turn {t}", doc, False
            e["seg"] = contain
        cls, noise = e.get("classes"), e.get("noise")
        if not isinstance(cls, list) or not all(c in SIGNAL_TYPES for c in cls):
            return False, "SCHEMA_FAILURE", "bad classes", doc, False
        if not isinstance(noise, bool):
            return False, "SCHEMA_FAILURE", "noise not bool", doc, False
        if not cls and not noise:
            return False, "SCHEMA_FAILURE", "classes empty and noise=false (R8)", doc, False
        if cls and cls[0] != min(cls, key=lambda c: PREC_RANK[c]):
            return False, "SCHEMA_FAILURE", "classes[0] is not the precedence-primary", doc, False

    full_text = "\n".join(str(t.get("content", "")) for t in raw_chat_set)
    for a in doc["artifacts"]:
        if a.get("artifact_type") not in ARTIFACT_TYPES:
            return False, "SCHEMA_FAILURE", "bad artifact_type", doc, False
        if not isinstance(a.get("text"), str) or a["text"] not in full_text:
            return False, "ANCHOR_MISMATCH", "artifact text not a substring of raw content", doc, False

    ia = doc["intent_anchor"]
    if ia is not None:
        t = ia.get("turn") if isinstance(ia, dict) else None
        s = ia.get("seg") if isinstance(ia, dict) else None
        valid = (isinstance(t, int) and isinstance(s, int)
                 and 0 <= t < len(segs) and 0 <= s < len(segs[t]))
        if not valid:
            # Recompute from the anchored entries: first non-noise entry with
            # DIRECTIVE in classes[], else first non-noise entry with CONSTRAINT, else null.
            anchor_entry = None
            for e in doc["classified"]:
                if not e.get("noise") and "DIRECTIVE" in e.get("classes", []):
                    anchor_entry = e
                    break
            if anchor_entry is None:
                for e in doc["classified"]:
                    if not e.get("noise") and "CONSTRAINT" in e.get("classes", []):
                        anchor_entry = e
                        break
            if anchor_entry is None:
                ia = None
            else:
                ia = {"turn": anchor_entry.get("turn"), "seg": anchor_entry.get("seg")}
            doc["intent_anchor"] = ia

    st = doc["stack_inferred"]
    ev = st.get("evidence")
    if ev not in EVIDENCE_CONF:
        return False, "SCHEMA_FAILURE", "bad evidence", doc, False
    if abs(float(st.get("confidence", -1)) - EVIDENCE_CONF[ev]) > 1e-9:
        return False, "SCHEMA_FAILURE", "confidence does not match evidence table", doc, False
    if any(t not in user_turns for t in st.get("evidence_turns", [])):
        return False, "SCHEMA_FAILURE", "evidence_turns contains non-USER turns", doc, False
    if ev == "NONE" and (st.get("language") or st.get("framework")):
        return False, "SCHEMA_FAILURE", "evidence NONE but language/framework set", doc, False
    return True, "OK", "", doc, False


def signal_arrays(doc):
    arrays = {s: [] for s in SIGNAL_TYPES}
    for e in doc["classified"]:
        if not e.get("noise") and e.get("classes"):
            arrays[e["classes"][0]].append(e)
    return arrays


def derive_verdict(doc, duplicate_hash=False, signal_quality=None):
    arrays = signal_arrays(doc)
    if duplicate_hash:
        return True, "DUPLICATE_HASH", "ARCHIVE"
    if not any(arrays.values()):
        return True, "NO_SIGNAL", "ARCHIVE"
    if signal_quality is not None and signal_quality < 0.300:
        return True, "NOISE_THRESHOLD", "ARCHIVE"
    if arrays["CONFIRMATION"] and not arrays["QUESTION"]:
        return True, "ALREADY_RESOLVED", None
    if doc.get("out_of_domain") and not arrays["DIRECTIVE"]:
        return True, "OUT_OF_DOMAIN", "ARCHIVE"
    return False, None, None


def write_stage_output(soma, chatset_id, stage, doc, expected_hash):
    node_id = f"stage:{chatset_id}:{stage}"
    soma.create_node("StageOutput", {
        "id": node_id,
        "stage": stage,
        "chatset_id": chatset_id,
        "chatset_hash": expected_hash,
        "output": canonical_json(doc),
    })
    return node_id
