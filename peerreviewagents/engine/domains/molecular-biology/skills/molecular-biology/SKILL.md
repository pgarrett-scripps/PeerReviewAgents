---
name: molecular-biology
description: Review molecular and cell biology claims involving perturbations, protein interactions, pathways, localization, or context-specific expression.
---

# Molecular and cell biology review

Investigate a claim only when resolving a named molecule, interaction, pathway, or cellular context could change the review. First anchor the concern to a manuscript block. Resolve organism, gene or protein identifier, isoform or construct, cell type, perturbation, dose, duration, compartment, and assay.

Use generic manuscript search and citation tools to compare methods, figures, supplements, and cited evidence. Call `lookup_source` with the provider, exact identifier, and manuscript block ID. `uniprot` accepts a protein accession. `string` accepts an identity query such as `9606:TP53`. `string-interactions` accepts one exact unordered pair such as `9606:TP53,MDM2` and returns the combined score plus neighborhood, fusion, phylogenetic cooccurrence, coexpression, experimental, database, and text-mining channels. `reactome` accepts an exact stable record such as `R-HSA-69563` or its versioned form. It returns pathway or reaction identity, class, limited species and summation metadata. It does not return participants, hierarchy, diagrams, or pathway activity. IntAct, GO, and Expression Atlas have no active adapter. Do not substitute an unverified source.

For perturbations, distinguish depletion, knockout, inhibition, overexpression, rescue, and acute treatment. Ask whether the reagent is specific, whether the measured species is RNA, total protein, modified protein, activity, or phenotype, and whether rescue or orthogonal perturbation addresses off-target effects. A database annotation cannot repair an undercontrolled experiment.

For protein interactions, identify the exact claim:

- The `string` adapter resolves one identity. The `string-interactions` adapter checks one exact taxon and pair and exposes association channels. It does not establish direct binding, causal regulation, transfer status, or the relevance of an association to the manuscript conditions.
- IntAct has no active adapter. Frame the actionable follow-up around participants, constructs, interaction detection method, host system, publication, and whether the experiment tests direct interaction.
- Co-immunoprecipitation, proximity labeling, colocalization, genetic interaction, and coexpression do not by themselves establish direct binding.

A concern is rebutted when manuscript evidence matches the claimed entity and context, uses a method capable of supporting the stated relationship, and includes appropriate specificity controls. Downgrade a concern when an apparent mismatch is only a synonym or explicitly mapped isoform. Keep it unresolved when the accession, construct, assay direction, or cellular context cannot be reconciled.

Do not infer causality from association, universality from one cell line, pathway activation from membership, function from ontology ancestry, or endogenous relevance from overexpression. Current annotations also do not prove that the evidence existed at the review cutoff.
