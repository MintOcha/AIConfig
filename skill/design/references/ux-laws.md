# Laws of UX: practical decision guide

Source: [Laws of UX](https://lawsofux.com/laws/) (pages [2](https://lawsofux.com/laws/page/2/) and [3](https://lawsofux.com/laws/page/3/)); accessed 2026-07-30. This is a concise application guide, not a reproduction of the source.

Use these heuristics to explain observed behavior and compare design options. They are tendencies, not universal rules; validate consequential choices with users and product evidence.

## Attention and choice

- **Hick's Law / choice overload:** More or harder-to-compare options slow decisions. Prioritize, group, recommend sensible defaults, and disclose advanced choices later. Do not hide choices that people need to compare.
- **Selective attention:** People notice what serves their current goal. Make the next relevant action salient and remove competing noise.
- **Von Restorff effect:** A distinct item is remembered. Reserve visual isolation for the primary action, a changed state, or genuinely exceptional information; if everything shouts, nothing stands out.
- **Serial-position effect:** First and last items are recalled best. Put high-value or safety-critical items at meaningful edges, but do not use ordering to obscure material information.
- **Goal-gradient effect:** Motivation rises as completion feels near. Show truthful progress, preserve momentum, and make the next step concrete.
- **Zeigarnik effect:** Incomplete work remains mentally active. Preserve drafts and progress; use unfinished-state reminders carefully rather than manufacturing anxiety.

## Memory and complexity

- **Cognitive load / working memory:** Minimize what people must remember while acting. Keep needed context visible, chunk information, use recognition over recall, and avoid simultaneous demands.
- **Miller's Law:** Chunking is more useful than treating “7 ± 2” as a hard menu limit. Group meaningful units based on the task.
- **Chunking:** Break information into coherent, labeled groups that match how people scan and decide.
- **Tesler's Law:** Some complexity is irreducible. Move it to the system when reliable automation or good defaults can handle it; keep users in control where judgment is required.
- **Occam's Razor / Prägnanz:** Prefer the simplest explanation and form that preserves necessary meaning. Simplicity is not removing essential capability.
- **Pareto principle:** Make the common, high-value paths excellent and easy to reach, while keeping less frequent paths available.
- **Parkinson's Law:** Bound tasks and flows; reduce needless steps and avoid open-ended waiting.

## Familiarity and mental models

- **Jakob's Law:** Reuse common conventions because people spend most of their time in other products. Deviate only when the benefit exceeds the learning cost and affordances remain clear.
- **Mental models:** Name and organize the interface around what users believe they are manipulating, not the internal architecture.
- **Paradox of the active user:** Assume people will try the product before reading instructions. Make the first action discoverable, teach in context, and make early mistakes cheap.
- **Postel's Law:** Accept reasonable input variation and produce clear, consistent output. Do not apply “be liberal in acceptance” where ambiguity creates security or safety risk.

## Perception and grouping

- **Proximity:** Nearby elements appear related. Use spacing before extra borders.
- **Common region:** A boundary groups contents strongly. Use containers when a region has a real semantic or interactive role; avoid nested-card clutter.
- **Similarity:** Similar styling implies similar meaning or behavior. Keep component semantics consistent across the product.
- **Uniform connectedness:** Visible connections imply a stronger relationship. Use lines, paths, or shared surfaces for actual sequences and dependencies.
- **Aesthetic-usability effect:** Polish can improve perceived ease of use and tolerance for minor issues, but cannot repair a broken flow. Never let visual appeal conceal poor accessibility or misleading behavior.

## Action and feedback

- **Fitts's Law:** Make frequent and important targets comfortably large, close to the point of attention, and well spaced. Do not place destructive targets where slips are likely.
- **Doherty threshold:** Interaction feels fluid when feedback is fast (the source cites under 400 ms). Respond immediately with state change, optimistic UI only when safe, or honest progress feedback.
- **Peak-end rule:** People disproportionately remember an experience's emotional peak and ending. Make critical moments calm and the conclusion clear, confirmed, and useful.
- **Flow:** Reduce interruption and keep challenge, context, and feedback aligned with the task. Do not optimize “engagement” at the cost of agency.

## Responsible use

- Treat cognitive biases as safeguards for users, not tools for coercion.
- Avoid false urgency, hidden defaults, confirmshaming, obstruction, and progress manipulation.
- Pair heuristic review with accessibility checks, analytics, usability testing, and domain constraints.
