import assert from 'node:assert/strict';
import { apply } from '../dsh-plugin/index.js';

let resolvePython;
let spawnCount = 0;
let dispose;
const context = {
  get() { return undefined; },
  subprocess: {
    resolveExecutable() { return new Promise((resolve) => { resolvePython = resolve; }); },
    spawn() { spawnCount += 1; throw new Error('must not spawn after dispose'); },
  },
  effect(callback) { dispose = callback(); },
};
apply(context, { projectDir: '.', port: 9, autoStart: true });
for (let i = 0; i < 100 && !resolvePython; i++) await new Promise((r) => setTimeout(r, 10));
assert.equal(typeof resolvePython, 'function', 'Python resolution must begin');
dispose();
resolvePython('python');
await new Promise((r) => setTimeout(r, 30));
assert.equal(spawnCount, 0, 'disposed plugin must not launch child');
console.log('Host disposal during executable lookup: PASS');
