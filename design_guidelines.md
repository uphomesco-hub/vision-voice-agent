# Design Guidelines — Zeno AI

## Design System: Cal.com Dark Adaptation
Monochromatic dark design inspired by Cal.com's grayscale system.

### Colors
- Background: `#111111` (Midnight)
- Surface: `#1a1a1a` (Charcoal)
- Elevated surface: `#222222`
- Primary text: `#ffffff`
- Secondary text: `#898989`
- Muted text: `#5a5a5a`
- Borders: `rgba(255,255,255,0.06)` (shadow-based, not CSS borders)

### Typography
- Font: Inter (all weights 300-700)
- Headings: 28px weight 700, tight letter-spacing (-0.02em)
- Body: 13px weight 400-500
- Labels: 12px weight 600, uppercase, 0.04em tracking
- Micro: 10-11px weight 500-600

### Elevation
- Cards: Multi-layered shadows (contact + ring + diffuse)
- Buttons: Inset highlight (`rgba(255,255,255,0.15) 0px 2px 0px inset`)
- No gradients, no glow effects

### Components
- Buttons: White bg on dark, 8px radius, hover opacity 0.85
- Selects: Dark surface bg, shadow-ring border, custom dropdown arrow
- Panels: 14px 18px padding, border-bottom separators
- Pills: 9999px radius for badges

### Layout
- Session: Camera left (flex:1) + Info panels right (360px)
- Spacing: 8px base, generous (48px between sections)
- Controls bar: 48px circle buttons, centered
