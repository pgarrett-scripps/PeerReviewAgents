---
name: ecology-conservation
description: Review ecological, evolutionary, and conservation studies when taxon identity, detectability, sampling units, spatial or temporal dependence, range, or management inference affects the conclusions.
---

# Ecology, evolution, and conservation

Identify the taxon, accepted name and rank, population, sampling unit, site, observation unit, repeated measures, season, geography, effort, response, and claimed scale. Distinguish organisms, subsamples, plots, sites, and independent interventions.

The active `gbif` adapter accepts an exact numeric taxon key and returns species-record taxonomy. The distinct `gbif-occurrence` adapter accepts an exact numeric occurrence key such as `123456789`. It returns bounded identity, taxon keys, occurrence status, basis of record, event date, country, coordinates and uncertainty when supplied, dataset and collection identifiers, recorders, modification date, and the first 12 quality flags. It does not search occurrences, retrieve multimedia or extensions, or assess a range. Call `lookup_source` with the correct provider, exact key, and manuscript block ID. Ask for the exact record and provenance when those details are consequential.

Inspect effort, detectability, occupancy versus abundance, repeated visits, seasonality, spatial autocorrelation, site independence, pseudoreplication, missing visits, and the reference population. For management claims, distinguish randomized or replicated interventions from before-after patterns and examine spillovers and external events.

Evidence that can rebut a concern includes resolved synonymy, independent site replication, effort offsets, detection modeling, a suitable temporal comparison, coordinate uncertainty wholly inside the claimed region, or a claim narrowed to documented observations. Duplicate records or reused specimens are not independent corroboration.

Do not infer absence, nativeness, population trend, or management effectiveness from a few occurrence records. Do not download large occurrence sets. Treat specialized marine and environmental sources as unavailable until audited.
