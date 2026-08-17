"""Stage 2: liveness detection through `arbiter-engine`.

Everything in here needs the optional `[detect]` extra. Stage 1 does not import it,
deliberately — the coverage diff has to run on a bring-up bench with nothing
provisioned, and that property is easy to lose by accident and invisible until
somebody is standing in front of a machine that will not boot.

The generator itself has no engine dependency: it emits a model as plain data, so it
can be tested and golden-pinned without installing anything.
"""
