---
name: structural-biology
description: Review experimental and predicted structure claims involving constructs, residues, ligands, interfaces, assemblies, or validation.
---

# Structural biology review

Anchor the structural question to a manuscript block. Resolve PDB or model accession, experimental versus computed status, method, construct sequence, mutations, chain and entity identifiers, author and label numbering, ligand identity, assembly, and the exact residue or interface under discussion.

Use manuscript tools to inspect methods, construct diagrams, density or map claims, legends, supplements, and cited structures. The active RCSB adapter accepts an exact four-character PDB entry ID and returns entry-level experiment, accession, entity-container, and summary metadata. The active UniProt adapter accepts an exact protein accession and can check organism, entry identity, sequence length, and limited annotation metadata. Call `lookup_source` with provider `rcsb` or `uniprot`, the corresponding exact identifier, and the manuscript block ID. RCSB does not retrieve coordinates, chain-level sequence mapping, residue coverage, ligands, assemblies, or validation reports. When residue or interface interpretation needs absent structural detail, state the exact chain, residue range, assembly, ligand, validation field, or coordinate evidence needed as a future source task or author question.

Check three distinct layers:

- Sequence presence: the construct contains the residue or segment.
- Coordinate presence: atoms are modeled for it in the relevant chain and model.
- Local support: experimental density, ensemble behavior, occupancy, B factors, geometry, or deposited validation supports the interpretation.

Record experimental method and applicable quality measures. Resolution is not a universal quality score. For crystallography, consider R values, completeness, geometry, and local support. For cryo-EM, consider map-model fit and local resolution. For NMR, consider ensemble and restraint information. Treat biological assemblies, crystallographic contacts, and author or software assignments separately.

A concern is rebutted when the exact construct, chain, residue mapping, ligand, assembly, and local validation support the manuscript wording. A harmless numbering offset is benign only when the mapping is explicit. Missing coordinates, alternate conformations, low local confidence, or ambiguous assembly assignments require narrower language.

Computed models must remain labeled as predictions. Confidence scores estimate aspects of model reliability and do not establish ligand poses, oligomeric state, conformational change, direct interaction, or cellular mechanism. Do not perform folding, docking, molecular dynamics, or visual density adjudication in this review skill.
