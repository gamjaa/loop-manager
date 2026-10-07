"""The sole Manager source contract, kept separate from document sources."""
import json
import re

SOURCE = "manager:verify-inputs"
PURPOSE = "create-verify-inputs"
VERSION = 1
RULE = "Maintain scripts/verify-inputs.json from the complete project and actual verify scripts; preserve full verification and review missing inputs."
PREFIX = "MANAGER_REQUEST: "


def source_content(source):
    if source != SOURCE:
        raise ValueError("unregistered Manager source")
    from loop_manager_internal.verification.inputs import RULES
    return json.dumps({"source": SOURCE, "purpose": PURPOSE, "version": VERSION,
                       "rule": RULE, "input_rules": RULES}, sort_keys=True)


def declaration(text):
    lines = [line[len(PREFIX):] for line in text.splitlines() if line.startswith(PREFIX)]
    if len(lines) != 1:
        raise RuntimeError("Manager source requires one registered request declaration")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate Manager request field")
            value[key] = item
        return value
    try:
        value = json.loads(lines[0], object_pairs_hook=unique)
    except ValueError as error:
        raise RuntimeError("invalid Manager request") from error
    if (not isinstance(value, dict) or set(value) != {"id", "revision", "purpose", "version"}
            or value["purpose"] != PURPOSE or type(value["version"]) is not int
            or value["version"] != VERSION or not isinstance(value["id"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["id"])
            or not isinstance(value["revision"], str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value["revision"])):
        raise RuntimeError("Manager request purpose/rules mismatch")
    return value


def validate_scope(text, sources):
    if sources != [SOURCE] and tuple(sources) != (SOURCE,):
        raise RuntimeError("Manager source cannot justify product goals")
    value = declaration(text)
    # Use an exact bounded Purpose, leaving Implementation and AC to the Planner.
    purpose = re.search(r"^## Purpose\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if purpose is None or purpose.group(1).strip() != RULE:
        raise RuntimeError("Manager plan Purpose exceeds registered request")
    return value
