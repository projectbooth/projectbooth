// `icon` (gateway's /modules response, ultimately module.yaml's own `icon`
// field) is a free-form, unconstrained string — nothing in ModuleManifest or
// gateway enforces a value set; "any icon name the ui-shell's icon set
// recognizes" is only a comment in modules/_template/module.yaml, not an
// enforced contract. The only two real values observed anywhere in this repo
// are "puzzle" (the default) and "wave" (a test fixture). A small local map
// of hand-drawn glyphs, not an icon-library dependency: a library's hundreds
// of names wouldn't guarantee coverage of some future module's invented
// string either, so the fallback glyph below is doing the real correctness
// work regardless of map size — add a name here later if a genuinely common
// one shows up.

import type { ReactElement } from 'react'

function PuzzleGlyph() {
  return (
    <>
      <rect x="4" y="4" width="7" height="7" rx="1" />
      <rect x="13" y="4" width="7" height="7" rx="1" />
      <rect x="4" y="13" width="7" height="7" rx="1" />
      <circle cx="17" cy="17" r="4" />
    </>
  )
}

function WaveGlyph() {
  return (
    <path
      d="M2 12c2-4 4-4 6 0s4 4 6 0 4-4 6 0"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
    />
  )
}

function FallbackGlyph() {
  return <rect x="4" y="4" width="16" height="16" rx="3" fill="none" stroke="currentColor" strokeWidth="2" />
}

const GLYPHS: Record<string, () => ReactElement> = {
  puzzle: PuzzleGlyph,
  wave: WaveGlyph,
}

export function ModuleIcon({ icon }: { icon: string }) {
  const Glyph = GLYPHS[icon] ?? FallbackGlyph
  return (
    <svg viewBox="0 0 24 24" width={20} height={20} aria-hidden="true">
      <Glyph />
    </svg>
  )
}
