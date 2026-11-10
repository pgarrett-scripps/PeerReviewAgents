---
name: astronomy-space
description: Review astronomy and space-science manuscripts when object identity, coordinate frame or epoch, catalog version, cross-match tolerance, selection, upper limits, calibration, or observation provenance affects a claim.
---

# Astronomy and space science

Record object names and aliases, coordinates, frame, epoch, proper motion, uncertainty, catalog and version, observation ID, instrument, band, calibration, time, selection rule, and the claimed physical interpretation.

SIMBAD, VizieR, and ADS have no active adapters. Do not call `lookup_source` for them. Preserve the exact object names, originating catalog and version, coordinates, frame, epoch, proper motion, units, uncertainties, observation identifiers, references, and proposed match tolerance needed for a later source task. Ask for an alias-resolution or association record when identity is consequential. Propagate epoch or proper motion from supplied evidence when material.

Inspect whether cross-match radius is justified by uncertainties and source density, whether several catalog entries reuse one observation, and whether selection functions, censoring, completeness, and calibration support the population claim. Distinguish detections, marginal measurements, non-detections, and upper limits.

Evidence that can rebut a concern includes a resolved alias, epoch-corrected position, uncertainty-aware match, independent observations, explicit selection modeling, or calibrated upper-limit treatment. A catalog classification is evidence with provenance, not a definitive physical diagnosis.

Do not reduce telescope data, download surveys, count reused catalog products as independent observations, or infer absence from a catalog non-detection. Treat ambiguous nearby objects and unavailable token-gated literature as unresolved.
