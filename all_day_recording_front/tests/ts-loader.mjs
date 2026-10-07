import { readFile } from 'node:fs/promises'
import ts from 'typescript'

// The default test entry imports production TypeScript modules directly. Use
// the already declared compiler so npm test also works with the Node 20 LTS
// runtime installed on developer machines.
export async function load(url, context, nextLoad) {
  if (!url.endsWith('.ts')) return nextLoad(url, context)
  const source = await readFile(new URL(url), 'utf8')
  const result = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    fileName: new URL(url).pathname,
  })
  return { format: 'module', shortCircuit: true, source: result.outputText }
}
