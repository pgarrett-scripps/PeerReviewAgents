---
name: physiology
description: Review physiological measurements and computational models for context, equations, units, parameters, initial conditions, and provenance.
---

# Physiology review

Anchor the concern to a manuscript block. For measurements, resolve species, strain, sex, age, tissue or organ, preparation, state, temperature, medium, stimulation, measurement method, normalization, and time scale. For a mathematical model, resolve repository identifier and version, CellML version, source publication, variables, units, equations, parameter values, initial conditions, and imported components.

Use manuscript tools to compare methods, equations, tables, supplements, and cited reference ranges. CellML has no active adapter. Do not call `lookup_source` for it. Preserve the exact repository identifier, version, source publication, imported components, variables, units, equations, parameter set, and initial conditions needed for a later check. Evaluate the manuscript representation and state which external model evidence remains unavailable.

Trace each reported model quantity to a definition and unit. Check dimensional consistency, sign conventions, state versus algebraic variables, parameter provenance, and whether the manuscript changed a repository model. Distinguish fitted, measured, assumed, and inherited values. A numerically plausible curve does not identify a parameter or validate the mechanism.

A concern is rebutted when the model version, equations, units, parameters, and initial conditions agree with the manuscript, or when the manuscript clearly documents intentional changes. A unit scale conversion is benign when explicit and numerically consistent. Missing units, unstated initial conditions, silent parameter changes, or an inaccessible model remain unresolved.

Reference ranges rebut concerns only when preparation and measurement context are comparable. Do not generalize across species, temperature, anesthetic state, ex vivo and in vivo preparations, or normalization conventions without justification.

Perform static consistency review only. Do not simulate, fit parameters, reproduce figures, or validate solver behavior unless a separate reanalysis task is authorized. A valid CellML document can still encode unsupported biology.
