---
name: microbiology
description: Review microbial isolate identity, assemblies, antimicrobial resistance, virulence records, and pathogen clustering claims.
---

# Microbiology review

Anchor the issue to a manuscript block. Resolve organism and taxonomic authority, strain or isolate name, culture collection identifier, BioSample, BioProject, sequence run, assembly accession with version, source, collection date, location granularity, host, and relevant biosafety or contamination context.

Use manuscript tools to inspect culture conditions, identification method, assembly and quality-control methods, reference choice, typing scheme, susceptibility testing standard, and data availability. NCBI Datasets, NCBI Pathogen Detection, and BV-BRC have no active adapters. Do not call `lookup_source` for them or imply access. Preserve the exact BioSample, BioProject, run, assembly, strain, cluster, gene, and pipeline identifiers needed for a future record check. Ask whether the deposited record matches the organism, sample, date, location granularity, and analysis version when that evidence is unavailable.

For resistance claims, distinguish detected gene, allele, point mutation, predicted genotype, and measured phenotype. Compare organism, gene family, allele, coverage or identity thresholds, pipeline and reference version, drug, breakpoint standard, medium, and testing method. A database genotype can corroborate identity but cannot replace susceptibility testing.

For cluster or transmission claims, preserve the versioned cluster identifier, pipeline, reference, membership date, and sampling context. SNP or allele distance depends on pipeline and included isolates. Single-linkage membership may change as records are added. Epidemiology is required to infer an outbreak or direction of transmission.

A concern is rebutted when isolate identifiers, assembly version, quality indicators, pipeline scope, and phenotype or epidemiology match the wording. A strain alias is benign only when the accession linkage is explicit. Keep unresolved records with conflicting taxa, missing collection metadata, contaminated or fragmented assemblies, undocumented thresholds, or changing cluster membership.

Do not run assembly, taxonomic classification, metagenomics, phylogenetics, or AMR calling. Absence can reflect database scope, pipeline thresholds, or update timing. Avoid exposing sensitive location or patient information beyond what is needed to assess the claim.
