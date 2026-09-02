import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { matchV3Route } from '../v3/core/routeMatch.js'

const cases = []

function test(name, callback) {
  cases.push({ name, callback })
}

test('restores nested recording routes and selected tabs', () => {
  assert.deepEqual(
    matchV3Route('/recordings/session%20one', '?tab=evidence'),
    {
      name: 'session',
      path: '/recordings/session%20one',
      sessionId: 'session one',
      tab: 'evidence',
    },
  )
  assert.deepEqual(matchV3Route('/unknown'), {
    name: 'overview',
    path: '/',
    sessionId: null,
    tab: null,
  })
  assert.equal(matchV3Route('/reminders').name, 'reminders')
  assert.equal(matchV3Route('/people').name, 'people')
})

test('keeps the V3 frontend on one shared audio controller', () => {
  const app = readFileSync(new URL('../v3/App.vue', import.meta.url), 'utf8')
  const shell = readFileSync(
    new URL('../v3/components/AppShell.vue', import.meta.url),
    'utf8',
  )
  const media = readFileSync(new URL('../v3/core/media.ts', import.meta.url), 'utf8')
  const pageState = readFileSync(
    new URL('../v3/components/PageState.vue', import.meta.url),
    'utf8',
  )
  assert.equal(app.includes('getElementById'), false)
  assert.equal(shell.includes("path: '/lab'"), false)
  assert.equal((media.match(/new Audio\(/g) ?? []).length, 1)
  assert.equal(media.includes('audio.src ='), true)
  assert.equal(media.includes("audio.removeAttribute('src')"), true)
  assert.equal(pageState.includes('设备处于离线状态'), true)
})

for (const { name, callback } of cases) {
  callback()
  console.log(`ok - ${name}`)
}

console.log(`${cases.length} frontend unit tests passed`)
