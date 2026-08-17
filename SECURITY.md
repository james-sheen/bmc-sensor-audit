# Security

## Report privately

**Use GitHub's private vulnerability reporting** — the *Security* tab on this
repository, *Report a vulnerability*. That opens a channel visible only to the
maintainer.

**Do not open a public issue for anything in the list below.** For most projects
that advice is routine. Here it is load-bearing: the failure modes this project
has are ones where *the report itself is the disclosure*. A public issue saying
*running `capture` against my BMC wrote this to the file* has, by being filed,
published the thing that should not have been published.

If private reporting is unavailable to you for any reason, open a public issue
that says only **that** you have something to report and nothing about what it is.

## What counts as a security issue here

This tool reads a machine's declared sensor inventory and walks a live BMC over
Redfish. Two consequences shape the whole list:

**1. A Redfish walk returns hardware identity.** Serial numbers, part numbers,
asset tags, MAC addresses and UUIDs come back from a real chassis as a matter of
course. `capture` writes only the *parsed sensor set* for exactly this reason —
the parse is the redaction, and it is the default with no flag to turn it off. So:

- **Any path by which raw Redfish payload reaches a written file** is a security
  issue, whether through `capture`, an error message, a log line, or a traceback.
- **Any inventory value appearing in this repository** is a security issue,
  including in a test fixture. The hygiene check exists to catch this and its
  coverage is pattern-based, so it finds the shapes it knows and nothing else.
- **A gap in `tools/hygiene_check.py`** — a rule that does not fire on a hazard it
  claims to cover — is a security issue even with nothing currently leaking. One
  of its rules was dead from the day it was written and only a planted address
  revealed it; assume there are others.

**2. It authenticates to infrastructure.** Credentials are supplied by the caller
and never stored, but:

- **Credentials appearing in output**, including in a URL echoed to a log or an
  exception, are a security issue.
- **`--insecure` disabling certificate verification** is documented and
  deliberate, because BMCs ship self-signed certificates. If it can be reached
  when *not* requested, that is a security issue.

## What does not need private handling

Ordinary defects, wrong sensor counts, parse failures, crashes on malformed
configuration, and **defects found in upstream configuration data**. That last one
is the tool working: a contradiction in a public `entity-manager` file is already
public, and belongs upstream rather than here.

## What to expect

A single maintainer, no service-level commitment, and no bounty. You will get an
acknowledgement and an honest answer about whether and when it will be fixed —
including *not soon*, when that is true.

**There is no supported release yet.** This project has no tagged version and is
not on any package index, so there is no patched version to upgrade to. A fix
lands on the default branch, and the report will say so plainly rather than imply
a release process that does not exist.

## Scope

This repository only. It reads OpenBMC `entity-manager` configurations and speaks
Redfish, and is not affiliated with OpenBMC, DMTF, or any hardware vendor. A
vulnerability *in* a BMC, in OpenBMC, or in a vendor's firmware should go to that
project or vendor — not here. If this tool *surfaces* such a flaw, the flaw is
still theirs; report it to them, and by all means tell us the tool helped.
