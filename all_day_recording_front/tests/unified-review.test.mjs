import assert from 'node:assert/strict'
import { unifiedSections, recordingIdentity, coverageExplanation } from '../v3/core/unifiedReview.ts'
assert.deepEqual(unifiedSections.map(s=>s.key), ['people','activity','followup'])
assert.equal(recordingIdentity({participant:{kind:'unknown',label:'未确认',status:'unresolved'}}),'说话人身份未确认')
assert.equal(recordingIdentity({participant:{kind:'person',label:'合成已确认人物',status:'resolved'}}),'合成已确认人物')
assert.ok(coverageExplanation.includes('不是召回率'))
console.log('PASS unified review organization / unknown identity / coverage semantics')
