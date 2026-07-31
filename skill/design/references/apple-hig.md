# Apple Human Interface Guidelines: cross-platform quality floor

Official sources: [Human Interface Guidelines](https://developer.apple.com/design/human-interface-guidelines/), [design principles](https://developer.apple.com/design/human-interface-guidelines/design-principles), [accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility), [layout](https://developer.apple.com/design/human-interface-guidelines/layout), [typography](https://developer.apple.com/design/human-interface-guidelines/typography), [color](https://developer.apple.com/design/human-interface-guidelines/color), and [motion](https://developer.apple.com/design/human-interface-guidelines/motion); accessed 2026-07-30.

Use the HIG directly for Apple-platform implementation details and current specifications. Apply its durable principles to other platforms while respecting their own conventions.

## Design principles

Apple's current principles can be operationalized as:

- **Purpose:** Identify what matters to people and make the core job excellent.
- **Agency:** Let people choose how to act, keep them informed, and make mistakes recoverable.
- **Responsibility:** Prioritize safety, privacy, transparency, and people's interests.
- **Familiarity:** Build consistently on physical and digital patterns people already understand.
- **Flexibility:** Support diverse contexts, devices, inputs, abilities, and perspectives.
- **Simplicity:** Be direct; include what is necessary and establish a clear hierarchy. Simplicity is not minimalism.
- **Craft:** Iterate, test in real settings, and care for wording, visuals, motion, audio, reliability, and performance.
- **Delight:** Choose an appropriate emotion and create character without obstructing the task; delight is not decoration.

## Accessibility

Design accessibility from the start. An accessible interface is intuitive, perceivable through more than one channel, and adaptable to how a person uses their device.

- Support text enlargement and reflow. Apple's HIG recommends allowing at least 200% enlargement where applicable and using platform text styles such as Dynamic Type.
- Maintain sufficient foreground/background contrast. Apple's current guidance cites WCAG AA ratios of 4.5:1 for normal text and 3:1 for sufficiently large text; verify the exact applicable standard and state.
- Never rely on color alone. Pair it with labels, shapes, icons, patterns, or position.
- Provide programmatic names, roles, states, logical reading order, and useful descriptions for nontext content.
- Support keyboard-only operation and visible focus. Do not override established system shortcuts without strong cause.
- Offer alternatives to complex gestures and audio-only or motion-only cues.
- Keep targets comfortable and separated. Apple's current default touch target for iOS/iPadOS is 44 × 44 pt; use the platform's current target guidance rather than blindly applying that number everywhere.
- Respect system settings for text size, contrast, appearance, captions, reduced motion, and other assistive preferences.
- Write clear labels, concise instructions, and actionable errors to reduce cognitive load.

## Layout and input

- Preserve hierarchy and context while adapting to window size, orientation, safe areas, localization, content size, and input method.
- Place important content first and keep primary controls easy to reach, without assuming one hand, pointer, or viewport.
- Use consistent alignment and a spacing rhythm to express relationships.
- Allow content to drive component height; test long labels, large text, empty data, and dense data.
- Prefer familiar platform components because they inherit interaction, accessibility, and adaptation behavior.
- Give every interactive element an obvious affordance and immediate feedback for hover, focus, press, selection, disabled, loading, success, and failure when relevant.

## Typography and color

- Establish a small semantic type hierarchy and keep body text comfortably readable. Preserve legibility across weights, sizes, contrast modes, and text enlargement.
- Use font roles deliberately; avoid using weight or color as the only hierarchy signal.
- Use semantic, adaptive colors where the platform or design system supplies them. Test light, dark, increased-contrast, and disabled states.
- Use color judiciously to communicate brand, status, continuity, and relationships. Keep it secondary to content comprehension.

## Motion

- Add motion only when it explains spatial continuity, confirms an action, communicates status, or directs attention.
- Keep feedback animation brief and precise, especially for frequent actions.
- Make motion optional and never its sole communication channel. Respect reduced-motion preferences.
- Let people interrupt or continue without waiting for nonessential animation.
- Keep motion spatially consistent with the gesture and resulting state; physically implausible transitions can disorient.

## Platform note

Do not make a web product imitate iOS cosmetically. Borrow the underlying principles, then follow web conventions, semantic HTML, browser behavior, and WCAG. For a native Apple product, consult the component- and platform-specific HIG pages before implementation because specifications change.
