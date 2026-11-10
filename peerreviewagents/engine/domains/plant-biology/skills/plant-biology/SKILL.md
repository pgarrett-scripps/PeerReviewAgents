---
name: plant-biology
description: Review plant biology claims involving gene identity, cultivar or ecotype, genome releases, homology, expression, or phenotype.
---

# Plant biology review

Anchor the question to a manuscript block. Resolve species, cultivar, accession or ecotype, ploidy, subgenome where relevant, genome assembly and annotation release, gene or transcript identifier, organ, developmental stage, growth conditions, treatment, and phenotype ontology.

Use manuscript tools to inspect plant material, growth and sampling conditions, gene nomenclature, transformation background, tissue definition, and deposition statements. The active `ensembl` adapter accepts one exact Ensembl stable gene or transcript ID, optionally with a numeric version. It returns object type, species, assembly, biotype, source, genomic region and coordinates, strand, version, canonical transcript when present, display name, and description. It does not return sequence, child features, homology, expression, phenotype, or cultivar-specific variation. Call `lookup_source` only when the manuscript supplies a compatible exact identifier. Expression Atlas and TAIR have no active adapters. Preserve their exact experiment or accession for a future check, and do not assume or redistribute subscription content.

For gene identity, preserve release-specific identifiers and distinguish homeologs, paralogs, splice isoforms, and renamed loci. For homology, record method, species set, relationship type, duplication context, and synteny when available. Sequence similarity or one-to-one labels do not establish conserved expression, biochemical function, or phenotype.

For expression, compare organ ontology, developmental stage, genotype, treatment, photoperiod, temperature, sampling time, assay, and normalization. An atlas pattern can corroborate broad context but does not reproduce the manuscript growth regime. For phenotype, distinguish direct observation, curated annotation, computational inference, and transfer from another species.

A concern is rebutted by an exact identifier and release match plus evidence from the relevant cultivar, tissue, stage, and condition. A renamed Arabidopsis locus is benign when the mapping is explicit. Keep polyploid homeolog ambiguity, cultivar-specific structural differences, obsolete coordinates, and mismatched developmental stages unresolved.

Do not generalize Arabidopsis evidence to crops without an explicit orthology and context argument. Do not infer function from expression alone or rerun genome annotation, differential expression, or quantitative genetics analyses.
