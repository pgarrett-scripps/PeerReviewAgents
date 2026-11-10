---
name: neuroscience
description: Review neuroscience claims involving atlases, neuronal morphology, anatomical coordinates, cell types, or preparation-specific measurements.
---

# Neuroscience review

Anchor each question to a manuscript block. Resolve species, strain, sex, age or developmental stage, brain region and hierarchy, hemisphere, cortical layer, cell-type taxonomy, preparation, coordinate space and version, assay, sampling unit, and any transformation between atlases.

Use manuscript tools to inspect stereotaxic methods, anatomical definitions, registration procedures, inclusion criteria, recording conditions, and figure legends. NeuroMorpho, Expression Atlas, and brain-atlas providers have no active adapters. Do not call `lookup_source` for them. Preserve the exact reconstruction, experiment, atlas, coordinate space, version, region, cell class, species, preparation, and transformation needed for later verification. Ask for those identifiers or a cited metadata record when they are necessary to interpret the comparison.

For morphology, compare species, region, cell class, staining or labeling method, reconstruction completeness, slicing and shrinkage, age, and reported morphometry. A repository match can show that a reconstruction and its metadata exist. It cannot establish that repository neurons represent the manuscript cohort or that reconstruction artifacts are negligible.

For atlas and expression claims, identify native coordinate space, registration target, anatomical ontology, assay platform, normalization, and whether values represent cells, nuclei, sections, subjects, or aggregates. Cross-species homologous labels and similarly named cell types require an explicit mapping.

A concern is rebutted when the exact dataset or reconstruction matches the relevant biological and coordinate context and the manuscript limits its inference accordingly. A label synonym is benign when the ontology mapping is explicit. Keep differences in age, preparation, atlas version, cell taxonomy, or platform unresolved when they could alter the conclusion.

Do not infer an individual-level result from an atlas average, connectivity from colocalization, function from morphology alone, or comparable abundance across platforms without calibration. Do not perform image registration, spike sorting, connectome reconstruction, or raw electrophysiology analysis.
