import { describe, expect, it } from 'vitest'
import { changesSince, describeChanges, directStages, formatBytes, formatSeconds, jobStages } from './exportView'

// Step 3 shows what the server reports, stage by stage: these are the rules
// that turn its pipeline into what the reader sees.

const stateOf = (stages: ReturnType<typeof jobStages>) => Object.fromEntries(stages.map(stage => [stage.id, stage.state]))

describe('the stages of a reviewed build', () => {
  it('follows the stage the server is in, with the counts it reports', () => {
    const stages = jobStages('running', { stage: 'chapters', running: true,
      units: { total: 500, reused: 495, to_narrate: 5, narrated: 5 }, chapters: { total: 12, done: 4, reused: 3, rebuilt: 1 },
      timings: { checking: 1.5, narrating: 40 } })
    expect(stateOf(stages)).toEqual({ prepare: 'done', check: 'done', narrate: 'done', chapters: 'active',
      package: 'pending', validate: 'pending', zip: 'pending', ready: 'pending' })
    const by = Object.fromEntries(stages.map(stage => [stage.id, stage]))
    expect(by.check.detail).toBe('495 de 500 frases sin cambios: su audio y su revisión se reutilizan')
    expect(by.narrate.detail).toBe('5 de 5 frases nuevas o corregidas')
    expect(by.narrate.seconds).toBe(40)
    expect(by.chapters.detail).toBe('4 de 12 · 3 sin cambios, reutilizados · 1 montados de nuevo')
    expect(by.chapters.progress).toEqual({ value: 4, max: 12 })
  })

  it('says nothing had to be narrated when everything was reused', () => {
    const stages = jobStages('completed', { stage: 'completed', outcome: 'completed',
      units: { total: 20, reused: 20, to_narrate: 0, narrated: 0 },
      timings: { checking: 0.2, narrating: 0, chapters: 1, package: 1, validating: 1, zipping: 1, verifying: 0.1 } })
    const narrate = stages.find(stage => stage.id === 'narrate')!
    expect(narrate.state).toBe('skipped')
    expect(narrate.detail).toContain('Nada que generar')
    expect(stages.find(stage => stage.id === 'zip')!.seconds).toBeCloseTo(1.1)
    expect(stages.every(stage => stage.state !== 'pending' && stage.state !== 'active')).toBe(true)
  })

  it('marks where a failed build stopped', () => {
    const stages = jobStages('failed', { stage: 'failed', outcome: 'failed', timings: { checking: 1, narrating: 2, chapters: 3 } })
    expect(stateOf(stages)).toMatchObject({ narrate: 'done', chapters: 'failed', package: 'pending' })
  })

  it('stops at the narration when paragraphs wait for a person', () => {
    const stages = jobStages('partially_completed', { stage: 'review', outcome: 'review',
      units: { total: 10, reused: 8, to_narrate: 2, narrated: 2 }, timings: { checking: 1, narrating: 2 } })
    const narrate = stages.find(stage => stage.id === 'narrate')!
    expect(narrate.state).toBe('failed')
    expect(narrate.detail).toBe('Hay frases que necesitan tu decisión')
  })

  it('starts with the preparation when the server has said nothing yet', () => {
    expect(stateOf(jobStages('queued', null))).toMatchObject({ prepare: 'active', check: 'pending' })
  })
})

describe('the stages of a direct download', () => {
  it('shows the narration progress with what was already narrated', () => {
    const stages = directStages({ state: 'running', stage: 'narrating', total: 5, done: 2, reused: 1 })
    const narrate = stages.find(stage => stage.id === 'narrate')!
    expect(narrate.state).toBe('active')
    expect(narrate.progress).toEqual({ value: 2, max: 5 })
    expect(narrate.detail).toBe('2 de 5 párrafos · 1 ya narrados, sin coste')
  })

  it('reaches «ready» when the task is completed', () => {
    const stages = directStages({ state: 'completed', stage: 'done', total: 3, done: 3, reused: 3,
      chapters_total: 2, chapters_done: 2, chapters_reused: 2, timings: { assembling: 0.5 } })
    expect(stages.find(stage => stage.id === 'ready')!.state).toBe('done')
    expect(stages.find(stage => stage.id === 'narrate')!.state).toBe('skipped')
    expect(stages.find(stage => stage.id === 'chapters')!.detail).toBe('2 de 2 · 2 sin cambios, reutilizados')
  })
})

describe('what changed since the last ZIP', () => {
  const produced = [
    { title: 'Uno', units: [{ text: 'A.' }, { text: 'B.' }] },
    { title: 'Dos', units: [{ text: 'C.' }] },
    { title: 'Tres', units: [{ text: 'D.' }] },
  ]

  it('counts removed and added chapters and changed phrases', () => {
    const current = [
      { title: 'Uno', units: [{ text: 'A corregida.' }, { text: 'B.' }, { text: 'Nueva.' }] },
      { title: 'Tres', units: [{ text: 'D.' }] },
      { title: 'Cuatro', units: [{ text: 'E.' }] },
    ]
    const changes = changesSince(produced, current)
    expect(changes).toEqual({ removedSections: ['Dos'], addedSections: ['Cuatro'], newPhrases: 2, removedPhrases: 1 })
    expect(describeChanges(changes)).toBe(
      '1 capítulo eliminado («Dos») · 1 capítulo añadido («Cuatro») · 2 frases nuevas o corregidas · 1 frase quitada')
  })

  it('finds nothing when nothing changed', () => {
    expect(describeChanges(changesSince(produced, produced))).toBe('')
  })
})

describe('numbers as people read them', () => {
  it('formats durations and sizes', () => {
    expect(formatSeconds(0.4)).toBe('<1 s')
    expect(formatSeconds(42.4)).toBe('42 s')
    expect(formatSeconds(125)).toBe('2 min 05 s')
    expect(formatBytes(512 * 1024)).toBe('512 KB')
    expect(formatBytes(236.4 * 1024 * 1024)).toBe('236,4 MB')
  })
})
