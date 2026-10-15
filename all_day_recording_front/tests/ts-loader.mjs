import { readFile } from 'node:fs/promises'
import ts from 'typescript'
import { parse, compileScript } from '@vue/compiler-sfc'

export async function resolve(specifier, context, nextResolve) {
  try { return await nextResolve(specifier, context) }
  catch (error) {
    if (specifier.startsWith('.') && !/\.[a-z]+$/i.test(specifier)) {
      return nextResolve(`${specifier}.ts`, context)
    }
    throw error
  }
}

// The default test entry imports production TypeScript modules directly. Use
// the already declared compiler so npm test also works with the Node 20 LTS
// runtime installed on developer machines.
export async function load(url, context, nextLoad) {
  if (!url.endsWith('.ts') && !url.endsWith('.vue')) return nextLoad(url, context)
  let source = await readFile(new URL(url), 'utf8')
  if (url.endsWith('.vue')) {
    const { descriptor } = parse(source, { filename: new URL(url).pathname })
    source = compileScript(descriptor, { id: url, inlineTemplate: true }).content
  }
  source = source.replaceAll('import.meta.env', '({ VITE_V3_MOCK: "0" })')
  const result = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    fileName: new URL(url).pathname,
  })
  return { format: 'module', shortCircuit: true, source: result.outputText }
}
