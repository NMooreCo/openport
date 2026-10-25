// Runs one eval case for a TypeScript or JavaScript call site: import the module, call the export with
// the case's input (read from stdin), and print the result as JSON on a marker line.
//
// The app's own OpenAI SDK reaches OpenPort's local stand-in for the API through OPENAI_BASE_URL, so
// the app's code runs unchanged and its process never holds a Token Factory key.
//
// usage: node [--import tsx] node_runner.mjs <module path> <export name> [<JSON list of input keys>]
import { readFileSync } from "node:fs";
import Module, { register } from "node:module";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [modulePath, exportName, argKeys] = process.argv.slice(2);
const modulePathname = resolve(modulePath);
const moduleUrl = pathToFileURL(modulePathname).href;

// A function (or class) the module doesn't export is still at its top level, so it's exported as the module
// loads, once tsx or Node's type stripping has turned it into JavaScript: an ES module gets `export { name };`
// from a load hook, a CommonJS one `module.exports.name = name;` as it compiles. The file is never written.
const topName = exportName.split(".")[0];
const exported = new RegExp(
  `export\\s+(?:default\\s+)?(?:async\\s+)?(?:function\\*?|class|const|let|var)\\s+${topName}\\b|export\\s*\\{[^}]*\\b${topName}\\b`,
).test(readFileSync(modulePathname, "utf8"));
if (!exported) {
  const hooks = `
    let target, name;
    export async function initialize(data) { ({ target, name } = data); }
    export async function load(url, context, nextLoad) {
      const result = await nextLoad(url, context);
      if (url !== target || result.source == null || result.format === "commonjs") return result;
      return { ...result, source: \`\${Buffer.from(result.source).toString("utf8")}\\nexport { \${name} };\\n\` };
    }`;
  register(`data:text/javascript,${encodeURIComponent(hooks)}`, import.meta.url, { data: { target: moduleUrl, name: topName } });
  // tsx compiles a CommonJS module into `__filename=...;(()=>{ ... })()`: the export goes inside, where its names are.
  const compile = Module.prototype._compile;
  Module.prototype._compile = function (content, filename) {
    if (filename === modulePathname) {
      const line = `\n;module.exports.${topName} = ${topName};\n`;
      const end = content.startsWith("__filename=") ? content.lastIndexOf("})()") : -1;
      content = end >= 0 ? content.slice(0, end) + line + content.slice(end) : content + line;
    }
    return compile.call(this, content, filename);
  };
}

// Plain fetch calls to an LLM endpoint (a hard-coded https://api.openai.com/v1/chat/completions, say) reach
// OpenPort's stand-in too, as the SDKs do through their base URL settings.
const httpBase = process.env.OPENPORT_HTTP_BASE;
if (httpBase) {
  const send = globalThis.fetch;
  const origin = httpBase.split("/case/")[0];
  globalThis.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const target = url.startsWith(origin) ? null
      : /\/chat\/completions\b/.test(url) ? `${httpBase}/chat/completions`
      : /\/v1\/messages\b/.test(url) ? `${httpBase}/messages` : null;
    if (target === null) return send(input, init);
    return send(input instanceof Request ? new Request(target, input) : target, init);
  };
}

async function readStdin() {
  let data = "";
  for await (const chunk of process.stdin) data += chunk;
  return data;
}

// `Class.method`: a method of an exported class, on a new instance built with OPENPORT_INIT's arguments (none
// by default), or a static method on the class itself.
function entry(mod, name) {
  const [className, method] = name.includes(".") ? name.split(".", 2) : [null, name];
  if (className === null) return mod[name] ?? mod.default?.[name];
  const cls = mod[className] ?? mod.default?.[className] ?? (mod.default?.name === className ? mod.default : undefined);
  if (typeof cls !== "function") return undefined;
  if (typeof cls[method] === "function") return cls[method].bind(cls);
  const instance = new cls(...JSON.parse(process.env.OPENPORT_INIT || "[]"));
  return typeof instance[method] === "function" ? instance[method].bind(instance) : undefined;
}

try {
  const input = JSON.parse((await readStdin()) || "{}");
  const args = argKeys ? JSON.parse(argKeys).map((key) => input[key]) : [input];
  const mod = await import(moduleUrl);
  const fn = entry(mod, exportName);
  if (typeof fn !== "function") throw new Error(`${modulePath} has no exported function ${exportName}`);
  const output = await fn(...args);
  process.stdout.write(`\nOPENPORT_RESULT ${JSON.stringify(output ?? null)}\n`);
} catch (error) {
  process.stdout.write(`\nOPENPORT_ERROR ${JSON.stringify(String(error?.stack ?? error))}\n`);
  process.exitCode = 1;
}
