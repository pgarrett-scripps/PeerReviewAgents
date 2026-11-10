---
name: materials-crystallography
description: Review materials and crystallographic studies when composition, phase, processing, structure record, specimen independence, measurement conditions, or uncertainty affects property claims.
---

# Materials and crystallography

Build a sample-to-property map. Record composition, stoichiometry, phase, polymorph, defects or substitutions, synthesis and processing, specimen geometry, temperature, pressure, field, orientation, measurement method, calibration, uncertainty, and replicate independence.

COD, OPTIMADE, and Materials Project have no active adapters. Do not call `lookup_source` for them. Preserve the exact provider and structure identifier, deposition, publication, experimental or computed status, composition, phase, method, conditions, and version needed for a later record check. Ask the authors for an existing checkCIF report or stable structure citation when it is consequential. Do not treat a computed relaxed structure as an experimental reference.

Inspect phase identification, sample purity, processing dependence, batch and specimen replication, property units, normalization, measurement range, hysteresis, and uncertainty. For crystallography, examine supplied structure metadata and an existing checkCIF report. An alert is a question requiring interpretation, not an automatic invalidation. Do not upload an unpublished CIF.

Evidence that can rebut a concern includes an exact polymorph match, orthogonal phase evidence, independent specimens, condition-matched measurements, reported uncertainty, or a satisfactory author response to a validation alert. A literature value at another temperature, composition, or processing state is not a direct contradiction.

Do not rerun refinement, infer phase from a formula alone, merge provider results without provenance, or claim compliance from a checkCIF score. Return key-required and provider-unavailable states explicitly.
