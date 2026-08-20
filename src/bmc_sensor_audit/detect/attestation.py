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

__all__ = ["build_attestation", "ATTESTATION_FORMAT"]

ATTESTATION_FORMAT = "bmc-sensor-audit/attestation/1"


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
