#!/usr/bin/env node
/**
 * KubeSandbox batch runner for the Node.js component (doc §5.3).
 *
 * Mirrors the Python runner's contract (isolated namespace, dump final scope to
 * VAR_DUMP_PATH) with one real limitation worth being explicit about: `vm.createContext`
 * makes a plain object act as the script's global object, and top-level `var` (or an
 * implicit global assignment like `x = 1`) becomes a property of that object — but
 * top-level `let`/`const` bindings live in the script's lexical environment, which V8
 * does not expose as an inspectable object through any public Node API. So unlike
 * Python's exec()-namespace dump (which captures every top-level assignment regardless
 * of how it was declared), this runner only ever captures `var`/implicit-global
 * bindings. That's a real JS-semantics constraint, not a bug — documented here instead
 * of silently under-delivering on doc §5.3's "global variable dump" framing.
 *
 * stdout/stderr/stdin are left to this process (inherited) — the provisioner captures
 * stdout/stderr at the container level and feeds/EOFs stdin up front (doc §5.1), same
 * as the Python runner.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const VAR_DUMP_PATH = '/tmp/.kubesandbox_vars.json';

function isJsonSafe(value) {
  try {
    JSON.stringify(value);
    return true;
  } catch (err) {
    return false;
  }
}

function dumpContext(sandbox, ownKeys) {
  const dumped = {};
  for (const name of ownKeys) {
    if (name.startsWith('__')) continue;
    const value = sandbox[name];
    if (typeof value === 'function') continue;
    dumped[name] = isJsonSafe(value) ? value : String(value);
  }
  return dumped;
}

function main() {
  const sourcePath = process.argv[2];
  if (!sourcePath) {
    process.stderr.write('usage: node_runner.js <file>\n');
    return 2;
  }

  const source = fs.readFileSync(sourcePath, 'utf-8');
  const sandbox = {
    console,
    require,
    process,
    Buffer,
    setTimeout,
    setInterval,
    clearTimeout,
    clearInterval,
    __filename: sourcePath,
    __dirname: path.dirname(sourcePath),
  };
  const preExistingKeys = new Set(Object.keys(sandbox));
  const context = vm.createContext(sandbox);

  let exitCode = 0;
  try {
    vm.runInContext(source, context, { filename: sourcePath });
  } catch (err) {
    process.stderr.write((err && err.stack ? err.stack : String(err)) + '\n');
    exitCode = 1;
  } finally {
    const userKeys = Object.keys(sandbox).filter((k) => !preExistingKeys.has(k));
    try {
      fs.writeFileSync(VAR_DUMP_PATH, JSON.stringify(dumpContext(sandbox, userKeys)));
    } catch (dumpErr) {
      process.stderr.write(`kubesandbox_runner: failed to write variable dump: ${dumpErr}\n`);
    }
  }

  return exitCode;
}

process.exit(main());
