---
name: mathematics-computing
description: Provide limited methods review for mathematical and computing manuscripts when definitions, assumptions, proof scope, benchmark splits, leakage, comparison fairness, complexity, or uncertainty can be assessed from reported evidence.
---

# Mathematics and computing

This skill provides limited methods coverage. It does not execute code, reproduce a benchmark, formally verify a proof, or establish broad computational-paper support.

For mathematical claims, map definitions, assumptions, quantifiers, theorem statement, lemmas, proof dependencies, boundary cases, and claimed scope. Check that examples and corollaries satisfy the stated assumptions. Use bounded symbolic or numeric checks only as counterexample probes, not proof certification. A failed attempt to find a counterexample is not evidence of correctness.

For computing claims, map dataset and version, unit of splitting, preprocessing, labels, train, validation and test boundaries, hyperparameter selection, repeated entities, baseline implementation, metric, uncertainty, hardware, and complexity measure. Inspect subject, time, site, or family leakage, test-set reuse, unfair tuning, incomparable compute, and unsupported generalization.

Use `lookup_source` with provider `crossref` or `datacite`, an exact DOI, and a manuscript block ID when publication, dataset, or software-record identity is consequential. Both active adapters require a DOI. Metadata verifies identity and dates, not theorem truth, artifact contents, or implementation equivalence.

Evidence that can rebut a concern includes an assumption stated in scope, a split grouped at the true independent unit, untouched test data, matched tuning budgets, confidence intervals over independent runs or units, a proof step covering the boundary case, or a narrowed claim. Report missing code or data as a reproducibility limitation only when relevant. Do not run author code, request large datasets, benchmark systems, or claim a proof is valid from surface inspection.
