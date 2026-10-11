# Game Development Guidelines (Roblox Native)

How to build games on Roblox with clean, robust, and maintainable design.

---

## 1. Responsive UI: Relative Scale Architecture

Never hardcode absolute pixel sizes for primary UI elements. Hardcoded pixel offsets look giant on mobile and microscopic on high-resolution displays.

### The Relative Scale Rule
- **Use Scale, not Offset**: Always construct positions and sizes using Scale `UDim2.fromScale(xScale, yScale)` (i.e. `UDim2.new(scaleX, 0, scaleY, 0)`).
- **Formula for converting existing designs**:
  `ScaleX = OffsetX / Parent.AbsoluteSize.X`
  `ScaleY = OffsetY / Parent.AbsoluteSize.Y`
- **Dynamic Text Scaling**:
  - Set `TextScaled = true` on labels so text automatically fits its container across all resolutions.
  - Apply `UITextSizeConstraint` (`MinTextSize` and `MaxTextSize`) to prevent text from growing uncomfortably large on 4K monitors or shrinking to unreadable specks on small phones.
- **Aspect Ratio Locking**:
  - Apply `UIAspectRatioConstraint` to elements that must retain a fixed shape (such as square action buttons, circular player portraits, minimaps, and icons).
- **AnchorPoints**:
  - Always pair `AnchorPoint` with `Position` (e.g., `AnchorPoint = Vector2.new(0.5, 1)` and `Position = UDim2.fromScale(0.5, 0.98)` for bottom-docked action bars and vitals).

---

## 2. Modular & Component-Driven Design

Never copy decompiled or foreign engine logic 1:1. Avoid scattering individual offsets, timers, and trigger checks across disparate systems.

### Encapsulate into Cohesive Modules
- Unify related functionality into dedicated, reusable ModuleScript components (e.g., `Collectable`, `Weapon`, `StatusEffect`).
- Level-design friendly anchors: Use invisible physical parts (e.g., a `Center` part inside a model) as references for triggers or effects, allowing visual tuning directly in Studio without editing code.
- Zero desync: Shared components ensure that visual feedback (rings, highlights) and gameplay triggers (collision bounds) share the exact same coordinates and radius.

---

## 3. Build-Time vs Runtime Separation

- **Build time (`.build.luau`)**:
  - Creates the physical hierarchy: maps, props, static UI layouts, lighting configurations, collision bounds.
  - Runs once during compilation; never generates geometry repeatedly at runtime.
- **Runtime (`.server.luau`, `.client.luau`, `.module.luau`)**:
  - Drives state, gameplay loops, input handling, and property tweening.

---

## 4. Models, Transforms, and Assets

- **Pivots**: A model's `PrimaryPart` or `WorldPivot` defines its transform. When moving models with `PivotTo`, preserve their authored orientation:
  ```lua
  model:PivotTo(CFrame.new(position) * CFrame.Angles(0, yaw, 0) * model:GetPivot().Rotation)
  ```
- **Ground Alignment**: Always compute ground placement by raycasting or measuring the lowest bounding vertex to avoid floating or sunken props.
- **Assets Folder**: Use `@assets/` to import shared build modules or raw `.rbxm` models directly into `.build.luau` scripts.
