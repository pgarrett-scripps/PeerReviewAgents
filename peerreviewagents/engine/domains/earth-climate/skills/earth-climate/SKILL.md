---
name: earth-climate
description: Review earth, environmental, climate, and remote-sensing studies when product version, spatial reference, temporal coverage, baseline, calibration, aggregation, or uncertainty affects a claim.
---

# Earth, environment, and climate

Trace each central quantity to collection, product and version, variable, processing level, spatial reference, nominal and effective resolution, aggregation, unit, time window, baseline, calibration, missingness, and uncertainty.

The active `nasa-cmr` adapter accepts one exact collection concept ID such as `C1214470488-ASF`. It returns collection ID, title, dataset and short names, version, update and temporal fields when supplied, data center or organization metadata, and at most eight links. It does not retrieve granules, spatial polygons, science keywords, full summaries, variables, or data arrays. Call `lookup_source` with that exact collection ID and the manuscript block ID. NOAA has no active adapter. Preserve exact granule, station, variable, version, quality, time, and historical-state identifiers for future checks when collection metadata is insufficient.

Inspect whether spatial grids, coordinate reference systems, masks, resampling, temporal windows, cumulative and instantaneous measures, anomalies, and climatological baselines match the claim. Check whether coverage gaps or quality flags can be differential. Distinguish raw observations, retrievals, reanalysis, modeled products, and derived outputs.

Evidence that can rebut a concern includes an exact product citation, documented version mapping, coordinate transformation, matched baseline, quality-control treatment, calibration evidence, or sensitivity to resolution and missingness. Metadata can verify identity and coverage, not the correctness of downstream processing.

Do not download or recompute large products, substitute a nearby station without justification, use current data as a historical vintage, or infer physical causation from a spatial correlation alone. Return authentication and unavailable-version states explicitly.
