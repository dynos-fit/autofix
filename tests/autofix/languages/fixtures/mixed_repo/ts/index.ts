// Fixture TypeScript module for task-20260417-006 pipeline-integration
// tests. Contains one import and one exported function so that
// tree-sitter-typescript can parse it successfully.
import { strict as assert } from "node:assert";

export function greet(name: string): string {
  assert.ok(typeof name === "string");
  return `hello, ${name}`;
}

greet("world");
