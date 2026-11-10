---
name: proteomics-ptm
description: Review proteomics and post-translational modification claims involving deposits, peptides, protein inference, mass shifts, or site localization.
---

# Proteomics and PTM review

Anchor each investigation to a manuscript block and state whether the concern is deposition, peptide identity, protein inference, modification identity, site localization, quantification, or biological interpretation. Resolve organism, proteome or sequence release, accession, isoform policy, experimental construct, enzyme, residue numbering, acquisition type, search settings, decoy strategy, and reported error control.

Use manuscript tools to find the data availability statement, methods, thresholds, supplementary peptide tables, and site-level probabilities. The active PRIDE adapter accepts an exact PXD accession and returns project-level deposition metadata. The active UniProt adapter accepts an exact protein accession and returns identity, organism, sequence length and checksum, plus a limited annotation sample. Call `lookup_source` with provider `pride` or `uniprot`, the corresponding exact identifier, and the manuscript block ID. PRIDE does not list or inspect deposited files, and UniProt does not map peptide sequences. UniMod and restricted site databases have no active adapters. For peptide mapping or modification chemistry, preserve the exact peptide, accession and isoform policy, proteome release, modification name or mass, residue specificity, and localization evidence needed as a future lookup or author question.

For a PXD accession, compare public status, title, organism, instrument, sample design, and file types with the manuscript. Distinguish raw files, search results, and processed tables. Reviewer-only credentials or an embargoed record mean unavailable evidence, not a nonexistent deposit.

For peptides and sites, distinguish these levels:

1. The sequence can map to a protein under a declared database and isoform policy.
2. A peptide-spectrum match passes the stated identification threshold.
3. A mass shift is chemically compatible with a named modification.
4. Fragment evidence localizes the modification among candidate residues.
5. Quantification supports a condition difference with appropriate uncertainty.
6. Independent evidence supports a functional consequence.

Evidence at one level does not establish later levels. A concern is rebutted by a consistent accession and numbering map, an identifiable search space, explicit protein grouping, site-localization evidence, and suitable error control. A benign near-match includes mature versus precursor numbering when the offset is documented. Keep ambiguous shared peptides, undocumented database versions, and indistinguishable localization candidates unresolved.

Do not claim global peptide uniqueness from a bounded lookup. Do not rerun spectra, searches, false-discovery estimation, or quantification. A known database site does not validate the manuscript spectrum, abundance change, or mechanism.
