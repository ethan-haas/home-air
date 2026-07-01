import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Read the dashboard HTML
const htmlPath = join(__dirname, '..', 'docs', 'index.html');
const htmlContent = readFileSync(htmlPath, 'utf-8');

// Extract functions from HTML using regex and brace matching
function extractFunctionBody(name) {
  const startRegex = new RegExp(`function\\s+${name}\\s*\\(([^)]*)\\)\\s*\\{`);
  const startMatch = htmlContent.match(startRegex);

  if (!startMatch) {
    throw new Error(`Could not find function ${name} in HTML`);
  }

  const params = startMatch[1];
  const startIdx = startMatch.index;
  let braceCount = 0;
  let inFunction = false;
  let endIdx = startIdx;

  // Find the opening brace and count braces
  for (let i = startIdx; i < htmlContent.length; i++) {
    const ch = htmlContent[i];
    if (ch === '{') {
      braceCount++;
      inFunction = true;
    } else if (ch === '}') {
      braceCount--;
      if (inFunction && braceCount === 0) {
        endIdx = i;
        break;
      }
    }
  }

  // Extract body (content between braces)
  const fullCode = htmlContent.substring(startIdx, endIdx + 1);
  const body = fullCode.substring(fullCode.indexOf('{') + 1, fullCode.lastIndexOf('}'));

  return { params, body };
}

// Extract and create functions
const csvFunc = extractFunctionBody('parseCsvLine');
const hourFunc = extractFunctionBody('localHour');
const dayFunc = extractFunctionBody('dayKey');

const parseCsvLine = new Function(csvFunc.params, csvFunc.body);
const localHour = new Function(hourFunc.params, hourFunc.body);
const dayKey = new Function(dayFunc.params, dayFunc.body);

// ============================================================================
// Test: parseCsvLine
// ============================================================================

test('parseCsvLine: simple comma-separated values', () => {
  const result = parseCsvLine('a,b,c');
  assert.deepEqual(result, ['a', 'b', 'c']);
});

test('parseCsvLine: quoted field with comma inside', () => {
  const input = '123,76.5,"apply failed: timeout, retry"';
  const result = parseCsvLine(input);
  assert.equal(result.length, 3);
  assert.equal(result[0], '123');
  assert.equal(result[1], '76.5');
  assert.equal(result[2], 'apply failed: timeout, retry');
});

test('parseCsvLine: quoted field with escaped double quotes', () => {
  const input = 'name,"value with ""quotes"" inside",end';
  const result = parseCsvLine(input);
  assert.equal(result.length, 3);
  assert.equal(result[1], 'value with "quotes" inside');
});

test('parseCsvLine: mixed quoted and unquoted', () => {
  const input = 'a,"b,c",d,e';
  const result = parseCsvLine(input);
  assert.deepEqual(result, ['a', 'b,c', 'd', 'e']);
});

test('parseCsvLine: empty fields', () => {
  const input = 'a,,c';
  const result = parseCsvLine(input);
  assert.deepEqual(result, ['a', '', 'c']);
});

test('parseCsvLine: trailing comma', () => {
  const input = 'a,b,';
  const result = parseCsvLine(input);
  assert.deepEqual(result, ['a', 'b', '']);
});

// ============================================================================
// Test: localHour (America/New_York timezone pinning)
// ============================================================================

test('localHour: 2026-07-01T00:30:00Z is 20.5 in NY (8:30pm EDT)', () => {
  // UTC: 2026-07-01 00:30:00 = 2026-06-30 20:30:00 EDT (UTC-4)
  const ts = Math.floor(new Date('2026-07-01T00:30:00Z').getTime() / 1000);
  const h = localHour(ts);
  // 8:30pm = 20:30 = 20.5
  assert.equal(h, 20.5, `Expected 20.5, got ${h}`);
});

test('localHour: 2026-01-15T05:00:00Z is 0.0 in NY (midnight EST)', () => {
  // UTC: 2026-01-15 05:00:00 = 2026-01-15 00:00:00 EST (UTC-5)
  const ts = Math.floor(new Date('2026-01-15T05:00:00Z').getTime() / 1000);
  const h = localHour(ts);
  // midnight = 0.0
  assert.equal(h, 0.0, `Expected 0.0, got ${h}`);
});

test('localHour: 2026-01-15T05:30:00Z is 0.5 in NY (12:30am EST)', () => {
  // UTC: 2026-01-15 05:30:00 = 2026-01-15 00:30:00 EST (UTC-5)
  const ts = Math.floor(new Date('2026-01-15T05:30:00Z').getTime() / 1000);
  const h = localHour(ts);
  // 12:30am = 0:30 = 0.5
  assert.equal(h, 0.5, `Expected 0.5, got ${h}`);
});

test('localHour: noon EDT is 12.0', () => {
  // UTC: 2026-07-01 16:00:00 = 2026-07-01 12:00:00 EDT (UTC-4)
  const ts = Math.floor(new Date('2026-07-01T16:00:00Z').getTime() / 1000);
  const h = localHour(ts);
  // noon = 12.0
  assert.equal(h, 12.0, `Expected 12.0, got ${h}`);
});

test('localHour: 9pm EDT is 21.0', () => {
  // UTC: 2026-07-01 01:00:00 = 2026-06-30 21:00:00 EDT (UTC-4)
  const ts = Math.floor(new Date('2026-07-01T01:00:00Z').getTime() / 1000);
  const h = localHour(ts);
  // 9pm = 21.0
  assert.equal(h, 21.0, `Expected 21.0, got ${h}`);
});

// ============================================================================
// Test: dayKey (America/New_York timezone pinning)
// ============================================================================

test('dayKey: UTC just after midnight is still previous day in NY (during EDT)', () => {
  // UTC: 2026-07-01 00:30:00 = 2026-06-30 20:30:00 EDT (UTC-4) = still June 30 in NY
  const ts = Math.floor(new Date('2026-07-01T00:30:00Z').getTime() / 1000);
  const day = dayKey(ts);
  // Should be 2026-06-30 in YYYY-MM-DD format (not July 1)
  assert.equal(day, '2026-06-30', `Expected 2026-06-30, got ${day}`);
});

test('dayKey: UTC just before midnight is same day in NY', () => {
  // UTC: 2026-07-01 03:59:00 = 2026-06-30 23:59:00 EDT (UTC-4) = same day in NY (2026-06-30)
  const ts = Math.floor(new Date('2026-07-01T03:59:00Z').getTime() / 1000);
  const day = dayKey(ts);
  // Should be 2026-06-30
  assert.equal(day, '2026-06-30', `Expected 2026-06-30, got ${day}`);
});

test('dayKey: noon EDT is correct NY date', () => {
  // UTC: 2026-07-01 16:00:00 = 2026-07-01 12:00:00 EDT (UTC-4) = 2026-07-01 in NY
  const ts = Math.floor(new Date('2026-07-01T16:00:00Z').getTime() / 1000);
  const day = dayKey(ts);
  // Should be 2026-07-01
  assert.equal(day, '2026-07-01', `Expected 2026-07-01, got ${day}`);
});

test('dayKey: winter EST transition', () => {
  // UTC: 2026-01-15 05:00:00 = 2026-01-15 00:00:00 EST (UTC-5) = midnight in NY (2026-01-15)
  const ts = Math.floor(new Date('2026-01-15T05:00:00Z').getTime() / 1000);
  const day = dayKey(ts);
  // Should be 2026-01-15
  assert.equal(day, '2026-01-15', `Expected 2026-01-15, got ${day}`);
});

// ============================================================================
// Test: savings calculation uses ×PRICE (source-level guard)
// ============================================================================

test('HTML contains savings calculation with *PRICE multiplier', () => {
  // Check that the renderAnalytics function contains the corrected expression
  // The correct expression is: fanH*(KW.cool-KW.fan)*PRICE
  // This tests that the bug (missing *PRICE) stays fixed

  // Find the line that calculates savings
  const savingsMatch = htmlContent.match(/const\s+saved\s*=\s*fanH\s*\*\s*\(\s*KW\.cool\s*-\s*KW\.fan\s*\)\s*\*\s*PRICE/);
  assert.ok(savingsMatch, 'HTML should contain: const saved=fanH*(KW.cool-KW.fan)*PRICE');
});

test('HTML KW constants defined correctly', () => {
  // Verify the power constants are defined
  const kwMatch = htmlContent.match(/const\s+KW\s*=\s*\{\s*cool:\s*1\.1\s*,\s*turbo:\s*1\.5\s*,\s*fan:\s*0\.12\s*,\s*idle:\s*0\.05\s*\}/);
  assert.ok(kwMatch, 'KW constants should be defined with cool=1.1, turbo=1.5, fan=0.12, idle=0.05');
});

test('HTML PRICE constant is 0.15', () => {
  // Verify the price constant
  const priceMatch = htmlContent.match(/const\s+.*PRICE\s*=\s*0\.15/);
  assert.ok(priceMatch, 'PRICE should be defined as 0.15');
});
