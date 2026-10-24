// Runs one eval case for a TypeScript or JavaScript call site: import the module, call the export with
// the case's input (read from stdin), and print the result as JSON on a marker line.
//
// The app's own OpenAI SDK reaches OpenPort's local stand-in for the API through OPENAI_BASE_URL, so
// the app's code runs unchanged and its process never holds a Token Factory key.
//
// usage: node [--import tsx] node_runner.mjs <module path> <export name> [<JSON list of input keys>]
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [modulePath, exportName, argKeys] = process.argv.slice(2);

async function readStdin() {
  let data = "";
  for await (const chunk of process.stdin) data += chunk;
  return data;
}

try {
  const input = JSON.parse((await readStdin()) || "{}");
  const args = argKeys ? JSON.parse(argKeys).map((key) => input[key]) : [input];
  const mod = await import(pathToFileURL(resolve(modulePath)).href);
  const fn = mod[exportName] ?? mod.default?.[exportName];
  if (typeof fn !== "function") throw new Error(`${modulePath} has no exported function ${exportName}`);
  const output = await fn(...args);
  process.stdout.write(`\nOPENPORT_RESULT ${JSON.stringify(output ?? null)}\n`);
} catch (error) {
  process.stdout.write(`\nOPENPORT_ERROR ${JSON.stringify(String(error?.stack ?? error))}\n`);
  process.exitCode = 1;
}
