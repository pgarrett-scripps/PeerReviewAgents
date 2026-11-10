---
name: economics-social-science
description: Review economic and quantitative social research when identification assumptions, registration, survey populations, administrative definitions, dependence, units, or historical vintages affect the claims.
---

# Economics and quantitative social science

Extract the target population, treatment or exposure, comparator, outcome, unit, time horizon, assignment mechanism, and implied estimand. Separate descriptive, predictive, structural, and causal claims before evaluating the model.

For experiments, resolve an exact AEA RCT Registry or OSF identifier when supplied. The active OSF adapter accepts only a public OSF registration ID and returns registration attributes. Call `lookup_source` with provider `osf`, that exact ID, and the manuscript block ID. It does not retrieve attachments or amendment history. AEA has no active adapter. For AEA records, or absent OSF history, preserve the registry ID and ask for intervention arms, assignment, outcomes, sample and stopping rules, exclusions, subgroup plans, analysis plan, and dated amendments. Treat retrospective registration and unavailable version history explicitly.

For observational causal designs, test the manuscript's own identification argument. Difference-in-differences needs a credible untreated counterfactual trend and attention to timing and composition. Regression discontinuity needs a justified assignment threshold, continuity near it, and no consequential manipulation. Instrumental variables need relevance plus a defensible exclusion and independence argument, with the target population or local effect stated. Fixed effects do not remove time-varying confounding by themselves.

Inspect panel structure, repeated observations, clustering level, survey strata and weights, attrition, missingness, multiple outcomes, functional form, transformations, deflation, and units. Check administrative variable definitions, policy timing, anticipation, spillovers, and whether standard errors reflect the assignment or dependence structure. Do not demand weighting when the target is explicitly conditional and the design supports that target.

The active `worldbank` adapter accepts one exact indicator code such as `SP.POP.TOTL`. It returns indicator name, unit, source note, source organization, source identity, at most eight topics, and the API metadata update date. It does not return country-year population values, revisions, observations, or transformations. Call `lookup_source` with provider `worldbank`, the exact indicator code, and the manuscript block ID only to verify series identity and definition. Census, FRED, and ALFRED have no active adapters. Record their exact series or variable ID, concept, universe, geography, unit, frequency, seasonal adjustment, price basis, observation date, release vintage, and revision for a future task. A current series definition cannot verify what was available to a historical decision maker.

Evidence that can rebut a concern includes a pre-analysis plan, balanced assignment, design-specific diagnostics, a suitable falsification test, robust inference matched to assignment, exact data documentation, historical vintage evidence, or claims narrowed to the identified population and estimand. Do not equate a passed diagnostic with proof of all assumptions, infer misconduct, or rerun a full econometric pipeline.
