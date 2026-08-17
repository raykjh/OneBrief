# PUZZLE TELOS User Outcome V1

## Authority

This file records the user-approved product outcome for the first PUZZLE TELOS production test. It defines the intended game, non-negotiable gameplay rules, delivery boundary, and visual target. It does not prescribe KHALINOS's internal architecture, implementation order, milestone details, or technical mechanisms.

## Product Identity

- Product name: **PUZZLE TELOS**
- Native form: a new Unity game delivered as a playable Windows desktop application and its complete Unity project.
- Connectivity: completely offline. Do not add a server, account system, login, cloud save, telemetry, advertisements, store, payment system, multiplayer, or any other network dependency.
- Product language: English.

## Core Experience

Create a 3D turn-based stealth puzzle set in an ancient underground labyrinth. The player reads enemy movement, discovers a safe route, retrieves a seal, and escapes without being detected. Logical problem-solving must be more important than combat.

## Core Rules

- The game board uses a 9 x 9 tile grid as its default layout.
- After each player action, guards move according to defined patrol rules.
- Guard vision and the next movement direction are shown before the player commits an action.
- Movement, interaction, and waiting each consume a clear turn.
- The game includes keys, locked doors, traps, pressure plates, and an exit.
- Detection by a guard or stepping on a lethal trap causes failure.
- Undo and Restart are available.
- The player may use the red-thread ability once to return to a previous position.
- Completing a chamber without using the red-thread ability awards an additional rating.

## Approved Visual Outcome

The authoritative concept board is `Puzzle_Telos_Approved_Outcome_Board_V1.png`. It is a quality and product-direction target, not a pixel-perfect implementation specification.

The finished product must present a coherent premium indie-game identity across these observable states:

1. **Title** — the PUZZLE TELOS title, an ancient labyrinth entrance, the masked courier, the red thread, and clear primary navigation.
2. **Chamber Select** — readable handcrafted chamber cards, progression state, and completion ratings.
3. **Gameplay** — a legible 3D or isometric 9 x 9 tactical board, player, guards, projected vision, predicted patrol movement, seal or key objective, door, trap, exit, action state, Undo, and Restart.
4. **Mission Result** — an unmistakable completion state that separately communicates seal recovery, undetected completion, and whether the red thread remained unused.
5. **Options and Pause** — visually consistent functional surfaces rather than developer or placeholder panels.

Visual direction:

- Ancient subterranean labyrinth built from dark basalt, worn bronze, ash, red thread, torchlight, and deep teal shadows.
- A small masked courier and bronze sentinels must remain distinguishable at gameplay distance.
- Guard vision, predicted movement, interactive objects, failure, success, and selection states must be readable without relying on decorative text.
- Use crisp interface overlays over a dimensional environment. The result must look like a finished game rather than a debug grid, primitive-only prototype, generic template, or temporary functional shell.
- English only. No placeholder copy, missing glyphs, debug labels, or lorem ipsum.

KHALINOS may change exact pixel positions, spacing, typography, icons, camera angle, animation details, and technical UI structure when necessary. Those choices must preserve or improve the approved readability, atmosphere, coherence, and apparent finish.

## Deliberately Unspecified For This Test

The user has not fixed the number of chambers, save behavior, settings inventory, accessibility inventory, input scheme, tutorial structure, content length, or detailed level layouts. KHALINOS must disclose reasonable defaults for these decisions in the resulting completion contract rather than silently treating a minimal prototype as the intended finished product.

