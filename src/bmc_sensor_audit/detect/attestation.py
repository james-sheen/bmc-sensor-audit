"""A per-run record of what was checked, what was declined, and on what evidence.

The engine answers `check()` with findings and declines. That is enough for an exit
code and not enough for an audit, because it drops the numbers: a finding says
*reading exceeds critical threshold* and never says the reading was 51.2 against a
declared 50.0. `attest()` returns those, and this assembles them into an artifact a
run can be judged from afterwards.

**Three things it records, and the second and third are the point.**

*What was checked* is the easy half. *What was declined* is the half a compliance
reader actually needs: an axiom that could not be evaluated is not an axiom that
passed, and an artifact listing only findings reads as a clean bill of health for
every question nobody managed to ask. *What this record does not establish* is the
third, and it is carried verbatim from the engine rather than written here.

## The engine's own boundary, quoted rather than paraphrased

Every evidence entry `attest()` returns carries a `boundary` string. On 0.1.7 it
reads *engine-side evidence only; production attestation records are v0.2*. That is
the engine declining to be called an attestation service, and it belongs in the
artifact for the same reason a decline does. Paraphrasing it would make this file
the author of a disclaimer the engine wrote.

## Sequencing, which is a real contract and not an implementation detail

`attest()` requires `check()` to have run on the same session. Called first it
returns an envelope with `source: unavailable` and the reason *nothing checked yet:
call check first* -- an honest refusal, and one that would be easy to mistake for
*this run found nothing to attest*. So this module never calls `attest` speculatively:
it takes the problem types out of an envelope `check()` already produced.
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_attestation", "validate_attestation", "ATTESTATION_FORMAT"]

ATTESTATION_FORMAT = "bmc-sensor-audit/attestation/1"


def validate_attestation(artifact: Any) -> list[str]:
    """Everything wrong with this artifact, or an empty list.

    **This lived inline in a CI workflow, and that was the defect rather than an
    inconvenience.** Checking logic that exists only inside a `run:` block cannot be
    called by the person who receives the artifact, cannot be tested, and cannot be
    versioned alongside the thing it checks. A compliance reader was expected to
    trust a shape they had no way to verify.

    So the rule it enforces ships, the canary calls it, and a recipient can run the
    same check over an artifact somebody sent them. One validator, two callers.

    Returns problems rather than raising, so a caller reports all of them at once
    instead of fixing them one run at a time.

    **Deliberately does not import the engine.** An artifact is JSON, and validating
    one is a Stage 1 operation: a recipient auditing a file should not have to
    install a detection engine to read it.
    """
    problems: list[str] = []
    if not isinstance(artifact, dict):
        return [f"the artifact is {type(artifact).__name__}, not an object"]

    declared = artifact.get("format")
    if declared != ATTESTATION_FORMAT:
        problems.append(f"format is {declared!r}, this build reads "
                        f"{ATTESTATION_FORMAT!r}")

    shape: list[str] = []
    for key in ("findings", "not_checked", "evidence"):
        if not isinstance(artifact.get(key), list):
            shape.append(f"{key!r} is missing or is not a list")
    if shape:
        # Everything below indexes into these lists, so reporting a type error and
        # then every consequence of it names one fault four times.
        #
        # Tracked separately from `problems` on purpose: returning early whenever
        # ANY problem existed meant a wrong `format` -- the first check -- masked
        # every other one, so a file could be fixed and re-run repeatedly, learning
        # one fault at a time.
        return problems + shape

    if len(artifact["evidence"]) != len(artifact["findings"]):
        problems.append(
            f"{len(artifact['findings'])} finding(s) but "
            f"{len(artifact['evidence'])} evidence entr(ies); a finding without its "
            f"measurement is the thing this artifact exists to carry")

    for index, entry in enumerate(artifact["evidence"]):
        if not isinstance(entry, dict) or not isinstance(
                entry.get("measurement"), dict) or not entry["measurement"]:
            problems.append(f"evidence[{index}] carries no measurement")

    engine = artifact.get("engine")
    if not isinstance(engine, dict):
        problems.append("the 'engine' block is missing")
    else:
        # **Required only when there IS evidence to bound.** The boundary is the
        # engine's statement about what its evidence establishes, and it is read off
        # the evidence entries themselves -- so a clean board, which produces no
        # findings and therefore no evidence, has no boundary to carry and is not
        # defective for lacking one.
        #
        # Requiring it unconditionally made this validator reject a healthy machine,
        # which is the exact inversion the rest of this file is written to prevent.
        # Found by the clean-board test rather than by reading.
        if artifact["evidence"] and not engine.get("boundary"):
            problems.append(
                "the engine's own boundary statement is absent while evidence is "
                "present; the artifact must not claim more than the engine says it "
                "can support")
        if engine.get("schema_version") is None:
            problems.append(
                "engine.schema_version is absent; without it the artifact does not "
                "record which envelope contract the judgment was made under")

    for key in ("unattested", "unread_feeds"):
        if not isinstance(artifact.get(key), list):
            problems.append(
                f"{key!r} is missing or is not a list; it is how this artifact "
                f"accounts for what was NOT part of the judgment, and an absent "
                f"list reads as 'nothing was left out'")

    return problems


def build_attestation(session: Any, envelope: dict, describe: dict,
                      manifest: Any, *, target: str,
                      attest_fn: Any) -> dict:
    """Assemble the artifact from an envelope `check()` has already produced.

    `attest_fn` is passed in rather than imported, because this module must not
    import `arbiter_engine` at module scope -- Stage 1 runs on a bench with nothing
    provisioned, and an import here would make the whole CLI need the extra.
    """
    problem_types = []
    for finding in envelope.get("findings") or []:
        problem_type = finding.get("problem_type")
        if problem_type and problem_type not in problem_types:
            problem_types.append(problem_type)

    evidence: list[dict] = []
    unattested: list[str] = []
    for problem_type in problem_types:
        attested = attest_fn(session, problem_type).to_dict()
        meta = attested.get("meta") or {}
        if meta.get("source") == "unavailable":
            # Recorded, not dropped. A problem type the engine declined to attest is
            # a gap in the artifact, and an artifact that quietly omits it claims
            # completeness it does not have.
            unattested.append(f"{problem_type}: {meta.get('reason', 'unavailable')}")
            continue
        for entry in attested.get("evidence") or []:
            evidence.append(_render(entry, manifest))

    return {
        "format": ATTESTATION_FORMAT,
        "target": target,
        "engine": {
            "schema_version": (envelope.get("meta") or {}).get("schema_version"),
            # Verbatim from the engine. See the module docstring: this is the engine
            # declining to be called an attestation service, and it is not ours to
            # soften.
            "boundary": _boundary(evidence),
        },
        "checked": envelope.get("checked") or {},
        "findings": [_finding(f, manifest) for f in envelope.get("findings") or []],
        # The half a compliance reader needs. An axiom that could not be evaluated is
        # not an axiom that passed.
        "not_checked": [_decline(d, manifest)
                        for d in (envelope.get("not_checked")
                                  or envelope.get("declines") or [])],
        "evidence": evidence,
        "unattested": unattested,
        "unread_feeds": [
            f"{u.get('entity_id', '?')}.{u.get('property', '?')}"
            for u in (describe.get("unconsumed_observations")
                      or (describe.get("model") or {}).get(
                          "unconsumed_observations") or [])],
    }


def _boundary(evidence: list[dict]) -> str | None:
    for entry in evidence:
        if entry.get("boundary"):
            return entry["boundary"]
    return None


def _sensor(entity_id: str, manifest: Any) -> str:
    """The name on the board, not the sanitised entity type.

    An artifact naming `MB_U73_THERM_LOCAL_2` is one nobody can act on six months
    later, which is the whole failure mode a per-run record exists to avoid.
    """
    for sensor in getattr(manifest, "sensors", ()):
        if sensor.entity_type == entity_id:
            return sensor.declared_name
    return entity_id


def _finding(finding: dict, manifest: Any) -> dict:
    return {"sensor": _sensor(finding.get("entity_id", "?"), manifest),
            "entity_type": finding.get("entity_id"),
            "axiom": finding.get("axiom"),
            "severity": finding.get("severity"),
            "problem_type": finding.get("problem_type"),
            "statement": manifest.translate_finding(finding)}


def _decline(decline: dict, manifest: Any) -> dict:
    return {"sensor": _sensor(decline.get("entity_id", "?"), manifest),
            "axiom": decline.get("axiom"),
            "reason": decline.get("reason"),
            "detail": decline.get("detail")}


def _render(entry: dict, manifest: Any) -> dict:
    inner = entry.get("evidence") or {}
    return {"sensor": _sensor(entry.get("entity_id", "?"), manifest),
            "axiom": entry.get("axiom"),
            "problem_type": entry.get("problem_type"),
            "confidence": entry.get("confidence"),
            "boundary": entry.get("boundary"),
            # The numbers, which is the reason this artifact exists at all. Copied
            # rather than reshaped: `bound`, `threshold_type` and `value` are the
            # engine's vocabulary, and renaming them here would create a second
            # vocabulary for one set of facts.
            "measurement": dict(inner)}
