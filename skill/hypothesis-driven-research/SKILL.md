---
name: hypothesis-driven-research
description: Use when conducting automated empirical research, replicating papers, interpreting experimental results, diagnosing why a method succeeds or fails, designing follow-up experiments, or deciding whether evidence supports, rejects, explains, or justifies deploying a method.
---

# Hypothesis-driven research

## Purpose

Automated research must produce understanding, not a leaderboard of attempted methods.

A result establishes what happened under a protocol. It does not, by itself, establish why. Strong or weak performance starts the investigation; it does not end it.

## Research contract

For every method under study, keep four conclusions separate:

- **Faithfulness:** whether the tested implementation represents the source method.
- **Performance:** what happened under the stated evaluation and how uncertain it is.
- **Explanation:** which mechanism accounts for the result and what alternatives remain.
- **Use:** whether current evidence permits deployment or another operational decision.

Poor performance may block deployment without explaining the failure. Strong performance may justify further confirmation without proving the proposed story.

## Work as a scientist, not a search loop

Begin from the claim and reconstruct the method from primary sources. Check papers, supplements, corrections, official code, data documentation, assumptions, and evaluation details. Reproduce source behavior or invariants where possible before modifying the method.

After the initial comparison, generate multiple plausible explanations for the observed pattern. Derive predictions that differ between them. Design the smallest informative experiment that can change belief among those explanations while preserving a valid comparison.

The experiment form is deliberately not prescribed. Depending on the problem, useful evidence may come from ablation, substitution, restoration, perturbation, synthetic data, negative or positive controls, mediation, invariance, counterfactual construction, qualitative inspection, boundary cases, new measurements, or a new experimental design. Choose based on the causal structure and available evidence rather than copying a fixed checklist.

Prefer experiments with high expected information gain. An experiment is valuable when its possible outcomes would lead to different conclusions, not merely when it might improve the score.

## Learn from every attempt

Before running an experiment, record:

- the explanation being tested;
- credible alternatives;
- the observations each explanation predicts;
- what is held fixed;
- what result would weaken or falsify each explanation.

Afterward, update the explanation set. A failed attempt must remove, weaken, split, or refine a hypothesis, expose a fidelity or measurement problem, or reveal that the experiment could not discriminate. Do not respond to failure by blindly adding another model, parameter, or component.

Vague labels such as "overfitting", "nonstationarity", "objective mismatch", "small data", or "dataset inconsistency" are starting points. Turn them into concrete mechanisms with observable consequences.

## Continue autonomously

When code, data, compute, repository artifacts, or lawful public sources make the next discriminating experiment reachable, run it. Do not stop at a research plan, list of possible ablations, benchmark table, or failed acceptance gate.

Continue until:

1. one explanation survives serious attempts to falsify it and accounts for the important observations better than the live alternatives; or
2. the remaining explanations cannot be distinguished with reachable evidence.

In the second case, report the identification boundary precisely: which explanations remain equivalent, why current evidence cannot separate them, and what new observation or intervention would.

Finite empirical evidence does not provide absolute certainty. Never satisfy a demand for certainty by inventing a causal story. State the scope and assumptions of the explanation.

## Preserve evidence status

Diagnostic reuse of inspected data may improve understanding, but it does not make those data untouched validation again. Keep discovery, diagnosis, confirmation, and prospective evidence distinct. A historically convincing explanation and a deployment-ready result are separate achievements.

## Research record

Maintain a compact, evolving record rather than disconnected reports. It must let another researcher reconstruct:

- the claim and sources;
- material implementation choices and deviations;
- evaluation clocks and controls;
- observed results and uncertainty;
- competing explanations and their predictions;
- experiments performed in chronological order;
- how each result changed belief;
- the present explanation, its scope, and unresolved alternatives;
- the operational decision and the evidence needed to revisit it.

## Failure modes

Stop and correct course when research becomes:

- rejecting or accepting a method from one aggregate metric;
- tuning without a hypothesis that predicts the effect of the change;
- attaching a plausible narrative after seeing results;
- running a standard ablation checklist unrelated to the live explanations;
- changing several causal factors without a design that can separate them;
- repeating experiments without updating the explanation set;
- treating budget exhaustion as evidence about mechanism;
- claiming the source method failed before establishing implementation fidelity;
- using diagnostic data as fresh confirmation.

## Final output

Report the performance result, the explanation process, and the operational decision separately. Show the chain of evidence that eliminated or weakened alternatives. If the mechanism is unresolved, say so and continue with the next reachable discriminator rather than converting uncertainty into rejection.