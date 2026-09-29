# Distinctive frontend design

Source inspiration: Anthropic's official [frontend-design skill](https://github.com/anthropics/claude-code/blob/main/plugins/frontend-design/skills/frontend-design/SKILL.md) and [plugin page](https://claude.com/plugins/frontend-design); accessed 2026-07-30. This reference distills and adapts the approach rather than copying the skill.

## Ground the aesthetic in the subject

Name the concrete subject, audience, and page job before choosing a style. Mine the subject's real materials, language, tools, history, environment, and artifacts for visual cues. A distinctive interface feels inevitable for its content, not merely unusual.

Use the user's direction exactly when supplied. When the brief leaves an axis open, choose it deliberately rather than falling into a familiar AI-generated aesthetic.

## Build a visual thesis

- Make the opening or dominant region express the product's main idea, not a stock hero arrangement.
- Let typography carry personality through intentional display, body, and utility roles; set scale, width, weight, and spacing as a system.
- Make structure encode information. Dividers, numbering, labels, cards, and grids must express real grouping, sequence, comparison, or hierarchy.
- Select one signature element derived from the brief. It may be a composition, interaction, type treatment, illustration, data view, or material behavior.
- Spend visual risk in that signature and keep the surrounding UI restrained.
- Match execution complexity to the direction: maximalism needs orchestration; minimalism needs exceptional spacing, typography, and detail.

## Avoid interchangeable output

Challenge defaults such as:

- the same neutral sans-serif, purple gradient, rounded cards, glow, and three-column feature grid for every product;
- a giant metric plus gradient accent without subject-specific justification;
- decorative `01 / 02 / 03` labels when the content is not a sequence;
- arbitrary blobs, glass, grids, noise, marquees, and scroll reveals that convey nothing;
- generic claims, fake testimonials, vague statistics, and placeholder copy.

These devices are valid when the brief supports them. The test is specificity: could the choice move unchanged to a different industry? If yes, revise it.

## Two-pass design process

### Pass 1: direction

Create a compact plan:

1. Experience sentence and audience.
2. Content hierarchy and primary action.
3. Four to six semantic colors with actual values.
4. Display, body, and optional utility type roles with fallbacks.
5. Layout concept and responsive transformation.
6. One signature element and why it belongs to this subject.
7. Motion concept, or an explicit decision that motion adds no value.

Sketch structural alternatives before selecting one. Critique the plan for transplantable defaults, visual excess, and conflict with task clarity. Revise before coding.

### Pass 2: execution

Implement the selected plan faithfully. Reuse project primitives, centralize tokens, and keep CSS specificity predictable. Use real content and build meaningful states.

Render and inspect the result. Compare it with the brief at desktop and mobile sizes, then remove one unnecessary flourish. Verify focus visibility and reduced motion as baseline behavior.

## Interface writing

Treat words as functional design material:

- write from the user's side of the screen and name recognizable concepts;
- prefer specific, active verbs: “Save changes” rather than “Submit”;
- keep the action name stable through button, progress, result, and notification;
- use plain, brand-appropriate language without filler;
- make errors state what happened and the recovery step;
- make empty states invitations to a meaningful action;
- give each label, hint, example, and description one clear job.

## Critique prompts

- What is this interface unmistakably about before reading the logo?
- What is the single primary action, and does hierarchy make that honest?
- Which choice is memorable, and is it earned by the subject?
- Which element is decorative but semantically empty?
- Does the design remain coherent with long content, no data, errors, and small screens?
- Are familiarity, accessibility, performance, or comprehension being traded for novelty?
