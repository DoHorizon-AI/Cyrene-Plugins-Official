// Dump Navigator's operation facts by running its real functions, cut from the TypeScript source.
// Needs Node with --experimental-strip-types. These functions are not exported, so they cannot be imported.
// 中文：运行从源码切出的真实函数，导出 Navigator 的操作信息。

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [navigatorRoot, inputPath, outputPath] = process.argv.slice(2);
const input = JSON.parse(readFileSync(inputPath, 'utf8'));
// Remove block comments first, so a commented-out old copy cannot be run in place of the live code.
const source = (relative) => readFileSync(join(navigatorRoot, relative), 'utf8').replace(/\/\*[\s\S]*?\*\//gu, '');
const serve = source('harness/src/serve.ts');
const toolProviders = source('harness/src/tool-providers.ts');

function cutFunction(text, name) {
  const matches = [...text.matchAll(new RegExp(`^(?:export )?function ${name}\\(`, 'gmu'))];
  if (matches.length !== 1) throw new Error(`expected one top-level function ${name}, found ${matches.length}`);
  const start = matches[0].index;
  const end = text.indexOf('\n}\n', start);
  if (end < 0) throw new Error(`function ${name} has no top-level end`);
  return text.slice(start, end + 3).replace(/^export /u, '');
}

function outerClaimExpression(text) {
  const matches = [...text.matchAll(/^\s*const profileIsOuterClaim = ([\s\S]*?);$/gmu)];
  if (matches.length !== 1) throw new Error(`expected one profileIsOuterClaim, found ${matches.length}`);
  return matches[0][1];
}

const moduleText = `
type ConnectorResourceProjection = string;
type JsonValue = unknown;
const sha256 = (value: string) => 'digest';
const canonicalJson = (value: unknown) => JSON.stringify(value);
${cutFunction(serve, 'connectorOperationPolicy')}
${cutFunction(toolProviders, 'connectorScopeLock')}
export const outerClaim = (providerId: string) => {
  const hostBinding = { providerId };
  return ${outerClaimExpression(toolProviders)};
};
export { connectorOperationPolicy, connectorScopeLock };
`;

const scratch = mkdtempSync(join(tmpdir(), 'connector-probe-'));
try {
  const modulePath = join(scratch, 'navigator.ts');
  writeFileSync(modulePath, moduleText);
  const navigator = await import(modulePath);

  const policies = {};
  for (const provider of input.providers) {
    policies[provider] = {};
    for (const operation of input.operations) {
      try {
        policies[provider][operation] = navigator.connectorOperationPolicy(provider, operation);
      } catch (error) {
        if (!(error instanceof TypeError)) throw error;
        policies[provider][operation] = null;
      }
    }
  }

  // With no branch argument a locked operation throws, so a field this list misses cannot look unlocked.
  const repository = { repository_id: 7, repository: 'a/b' };
  const fields = ['branch', 'head_branch', 'expected_head_branch'];
  const scopeLocks = {};
  for (const operation of input.operations) {
    let locked = false;
    try {
      locked = navigator.connectorScopeLock('github', 'profile', operation, repository) !== undefined;
    } catch {
      locked = true;
    }
    if (!locked) {
      scopeLocks[operation] = null;
      continue;
    }
    const accepted = fields.filter((field) => {
      try {
        return navigator.connectorScopeLock('github', 'profile', operation, { ...repository, [field]: 'topic' }) !== undefined;
      } catch {
        return false;
      }
    });
    if (accepted.length !== 1) throw new Error(`cannot tell which field locks ${operation}`);
    scopeLocks[operation] = accepted[0];
  }

  const scopeLocksOutsideGithub = [];
  for (const provider of input.providers.filter((value) => value !== 'github')) {
    for (const operation of input.operations) {
      try {
        if (navigator.connectorScopeLock(provider, 'profile', operation, {}) !== undefined) {
          scopeLocksOutsideGithub.push([provider, operation]);
        }
      } catch {
        scopeLocksOutsideGithub.push([provider, operation]);
      }
    }
  }

  const outerClaim = {};
  for (const provider of input.providers) outerClaim[provider] = navigator.outerClaim(provider);
  writeFileSync(outputPath, JSON.stringify({ policies, scopeLocks, scopeLocksOutsideGithub, outerClaim }));
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
