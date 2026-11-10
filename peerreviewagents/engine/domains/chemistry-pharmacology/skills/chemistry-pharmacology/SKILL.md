---
name: chemistry-pharmacology
description: Review chemistry and pharmacology manuscripts when chemical identity, stereochemistry, assay conditions, characterization, concentration, reaction, or yield affects a claim.
---

# Chemistry and pharmacology

Map each central claim to an exact chemical entity and context. Record name, structure identifier, stereochemistry, salt or solvate, mixture status, formula, concentration basis, purity, dose, route, assay endpoint, units, conditions, comparator, and manuscript location.

The active PubChem adapter accepts an exact numeric CID and returns formula, molecular weight, IUPAC name, and InChIKey. The active ChEMBL adapter accepts an exact CHEMBL assay ID and returns assay metadata, not activity measurements. Call `lookup_source` with provider `pubchem` or `chembl`, the corresponding exact identifier, and the manuscript block ID. PubChem does not search names or expose enough structure detail to establish salt form or stereochemical equivalence by itself. Use these calls only for the returned fields. For activity comparison, ask for the compound record, target, organism, endpoint, units, conditions, and measured value from the manuscript or a future activity adapter. Do not silently promote a name, formula, or connectivity match to full identity. IC50, EC50, Ki, Kd, percent inhibition, and exposure are not interchangeable.

Inspect whether characterization supports the identity and purity claimed, whether concentrations and stoichiometry are internally consistent, and whether conversion, isolated yield, and selectivity are distinguished. Small local calculations may check explicit formula mass, dilution, equivalents, or yield. Preserve significant figures and uncertainty. A low yield is not a defect when accurately reported and adequate for the claim.

Evidence that can rebut a concern includes an unambiguous structure or identifier, matched assay metadata, orthogonal characterization, a stated salt conversion, suitable controls, or a correction of the claim to the measured endpoint. A database activity value from another construct, species, substrate, or protocol is context, not a contradiction.

Do not infer structure from a database name match, reconstruct spectra, propose routes, perform docking, or claim selectivity from one activity record. Treat unresolved names and unavailable assay detail as limitations.
