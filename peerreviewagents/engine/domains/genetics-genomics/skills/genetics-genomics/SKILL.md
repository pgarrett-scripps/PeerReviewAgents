---
name: genetics-genomics
description: Review genetics and genomics claims involving variants, assemblies, transcripts, public classifications, orthology, or sequence accessions.
---

# Genetics and genomics review

Start from a manuscript block and define the identity question before looking up a record. Preserve the reported variant string. Resolve species, assembly and patch, chromosome or accession, coordinate convention, reference and alternate allele, strand, transcript accession with version, exon, gene, and variant class. For structural variants, also resolve interval uncertainty and breakpoint representation.

Use manuscript tools to inspect methods, reference build, pipeline versions, filters, pedigree or cohort definition, and data availability. The active `ensembl` adapter accepts an exact stable gene or transcript ID such as `ENSG00000141510.20` or `ENST00000269305`. It returns object type, species, assembly, biotype, source, region, coordinates, strand, record version, canonical transcript when present, display name, and description. It does not return sequence, child features, variants, consequences, or homology. Call `lookup_source` with provider `ensembl`, the exact ID, and the manuscript block ID. NCBI Datasets and ClinVar have no active exact-record adapters. Record their exact accessions and required context as future tasks when needed.

For consequence claims, state which transcript produces the consequence and whether it is canonical, clinically selected, or experimentally relevant. A valid alternate transcript consequence is a benign near-match only when the manuscript does not depend on the stronger consequence. Never silently lift coordinates between assemblies.

For ClinVar, record VCV, RCV, or SCV accession and version, condition, classification type, submitter, review status, evaluation date, conflicts, assembly, and cited evidence. Compare the same variant-condition pair. ClinVar archives submissions and does not independently validate every classification.

A concern is rebutted by an exact assembly, allele, transcript, and condition match plus evidence appropriate to the claim. Keep it unresolved when left normalization, transcript choice, multiallelic representation, mosaicism, phasing, or structural-variant bounds could change the conclusion. Orthology evidence can support identity mapping but cannot by itself establish conserved function or phenotype.

Do not infer molecular effect from a predicted consequence, causality from association, patient diagnosis from a database classification, or absence from a missing record. Do not call variants, rerun association tests, infer ancestry, or compute polygenic scores.
