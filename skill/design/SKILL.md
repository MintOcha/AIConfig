---
name: design
description: Design, critique, redesign, or implement distinctive and usable digital interfaces. Use for websites, landing pages, dashboards, mobile or desktop app screens, components, design systems, prototypes, UI styling, UX flows, accessibility reviews, visual polish, and frontend work where layout, typography, color, motion, interaction, responsive behavior, or interface copy materially affect the result.
---

# Design

Create interfaces with a specific point of view, familiar interaction patterns, and an accessible quality floor. Treat product purpose and user comprehension as constraints on aesthetics, not competing concerns.

## Load the right references

- Read [ux-laws.md](references/ux-laws.md) when choosing information architecture, interaction density, grouping, targets, defaults, feedback, or flow.
- Read [apple-hig.md](references/apple-hig.md) for platform conventions, accessibility, adaptive layout, input, typography, color, and motion.
- Read [distinctive-frontend.md](references/distinctive-frontend.md) when creating or substantially restyling a visual interface.
- For an audit, load only the references relevant to the observed problems. For a new interface, read all three.

Treat the references as decision tools, not a checklist. User requirements, an existing product's conventions, and platform norms take precedence. When principles conflict, favor task completion, clarity, accessibility, and reversibility before novelty.

## Workflow

### 1. Establish the brief

Inspect the repository, existing interface, design system, screenshots, and real content before proposing changes. Preserve established tokens and components unless redesign is explicitly in scope.

Identify or infer:

- the product or subject, intended audience, and screen's single primary job;
- platform, input methods, breakpoints, technical constraints, and brand constraints;
- content hierarchy, primary action, risky actions, and important empty, loading, error, success, and disabled states.

Ask only when a missing answer would materially change the result. Otherwise state the assumption and proceed.

### 2. Choose a direction before coding

Define a compact design contract:

- **Experience:** the feeling and product promise in one sentence.
- **Hierarchy:** the ordered content and actions; remove or defer low-value choices.
- **Visual system:** 4–6 semantic colors, type roles and scale, spacing rhythm, shape, elevation, and responsive behavior.
- **Signature:** one memorable, subject-derived element or interaction. Spend boldness here and keep supporting UI disciplined.
- **Interaction:** familiar controls, feedback, recovery, keyboard behavior, and motion purpose.

For substantial layouts, compare at least two quick structural options in prose or a tiny ASCII wireframe. Reject any choice that could be transplanted unchanged into an unrelated product. Use real or realistic domain content, never generic feature-card filler.

### 3. Apply behavioral and platform constraints

Before implementation, check that:

- choices are chunked and progressively disclosed;
- related elements are grouped by proximity, region, similarity, or connection;
- frequent and important targets are easy to acquire;
- conventions match the user's mental model unless deviation has a clear benefit;
- status is visible, response feels immediate, and destructive actions are recoverable;
- information is not communicated by color, sound, gesture, or motion alone;
- layout adapts without losing hierarchy or context;
- controls work by keyboard and have visible focus; touch targets and spacing are comfortable;
- stretched bars and unnecessarily long or full-width buttons are avoided; size actions to their labels and place them near the content they affect. Treat full-width controls as a last resort for genuinely constrained mobile layouts or platform-mandated patterns, not a default composition device;
- text remains legible when enlarged and contrast meets the applicable standard;
- motion is brief, purposeful, cancellable where practical, and respects reduced-motion preferences.

### 4. Implement with the native system

Use the project's existing framework, component library, and tokens. Prefer semantic HTML and platform controls, then style them. Avoid rebuilding library primitives manually when a supported component already provides the needed behavior and accessibility.

Derive implementation values from the design contract. Centralize repeated values as tokens. Keep selector scope predictable and avoid specificity collisions. Build all meaningful states and responsive layouts, not only the ideal desktop screenshot.

Write interface copy from the user's perspective:

- name concepts by what people recognize and control;
- use specific verbs and sentence case;
- keep action vocabulary consistent from control to confirmation;
- make errors explain what happened and how to recover;
- make empty states direct people toward a useful next action.

### 5. Critique in rendered form

Run the interface and inspect it visually whenever the environment permits. Capture screenshots at representative desktop and mobile sizes; inspect rather than trusting source code alone. Exercise keyboard navigation, focus, overflow, long content, reduced motion, and key states.

Critique in this order:

1. task clarity and content hierarchy;
2. interaction, feedback, and recovery;
3. accessibility and responsive behavior;
4. visual coherence and subject specificity;
5. polish: spacing, typography, alignment, color, motion, and copy.

Revise material issues and render again. Remove decoration that carries no information or brand value. Do not announce baseline quality claims; demonstrate them in the result.

## Audit output

When asked only to review, do not modify files. Report findings by severity with concrete evidence, user impact, and a concise recommendation. Distinguish violations from subjective taste. Reference the relevant principle when it helps explain why a finding matters, but do not use named laws as a substitute for evidence.

## Completion criteria

Finish only when the primary flow works, the visual direction is coherent and specific to the brief, essential states exist, the interface adapts to its target sizes and inputs, and the relevant verification has passed. Summarize the design direction, implementation, and checks performed.
