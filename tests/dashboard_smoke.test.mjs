import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const htmlPath = join(__dirname, '..', 'docs', 'index.html');
const htmlContent = readFileSync(htmlPath, 'utf-8');

// Try to import jsdom
let jsdom = null;
try {
  const jsdomModule = await import('jsdom');
  jsdom = jsdomModule.JSDOM;
} catch (e) {
  // jsdom not installed, will use static fallback
}

if (jsdom) {
  // ========================================================================
  // Dynamic test with jsdom: load HTML + old status.json shape
  // ========================================================================

  test('dashboard renders without crash with old status.json shape', async () => {
    // Create a JSDOM instance with the HTML
    const dom = new jsdom(htmlContent, {
      url: 'https://example.com/',
      // Don't load external scripts (Chart.js, luxon, etc.)
      // but we need the inline script to run
      resources: 'usable',
      beforeParse(window) {
        // Mock fetch to return old-format status.json (missing new keys)
        const oldStatus = {
          updated: Math.floor(Date.now() / 1000),
          t_ethan: 71.2,
          t_office: 72.5,
          band: [70, 72],
          target: 71,
          outdoor: 85,
          humidity: 45,
          mode: 'cool',
          fan: 'high',
          turbo: 0,
          setpoint: 71,
          ac_running: 1,
          indoor_hum: 52,
          central_cool: 0
          // Missing: mismatch, cmd, confirmed, unconfirmed, applied, error
        };

        const emptyHistory = '';

        window.fetch = async (url) => {
          if (url.includes('status.json')) {
            return {
              json: async () => oldStatus,
              text: async () => JSON.stringify(oldStatus)
            };
          } else if (url.includes('history.csv')) {
            return {
              text: async () => emptyHistory
            };
          }
          throw new Error(`Unexpected fetch: ${url}`);
        };

        // Mock Chart.js globally (the dashboard tries to use it)
        window.Chart = function() {
          return {
            destroy: () => {}
          };
        };
      }
    });

    const { window } = dom;
    const { document } = window;

    // Wait a bit for the inline script to execute
    await new Promise(resolve => setTimeout(resolve, 100));

    // Check that the page rendered without crashing
    // The #cards div should be populated (renderCards was called)
    const cards = document.getElementById('cards');
    assert.ok(cards, 'Should have #cards div');

    // Should have card elements (8 cards normally)
    const cardElements = cards.querySelectorAll('.card');
    assert.ok(
      cardElements.length > 0,
      `Expected at least one card, got ${cardElements.length}`
    );

    // Check that updated timestamp was rendered
    const updated = document.getElementById('updated');
    assert.ok(updated, 'Should have #updated element');
    assert.ok(
      updated.textContent.length > 0 && updated.textContent !== 'updated —',
      'Should have rendered updated timestamp'
    );
  });

  test('dashboard renders with partial history.csv', async () => {
    // Test with a minimal history to ensure CSV parsing works with old shape
    const csvData = `ts,t_ethan,t_office,outdoor,humidity,target,band_low,band_high,setpoint,mode,fan,turbo,ac_running,applied,indoor_hum,central_cool,t_living,t_heather,error
1719820200,71.2,72.5,85,45,71,70,72,71,cool,high,0,1,1,52,0,70,68,
1719820800,71.5,72.3,84,46,71,70,72,71,cool,high,0,1,1,51,0,70,68,`;

    const dom = new jsdom(htmlContent, {
      url: 'https://example.com/',
      resources: 'usable',
      beforeParse(window) {
        const oldStatus = {
          updated: Math.floor(Date.now() / 1000),
          t_ethan: 71.2,
          t_office: 72.5,
          band: [70, 72],
          target: 71,
          outdoor: 85,
          humidity: 45,
          mode: 'cool',
          fan: 'high',
          turbo: 0,
          setpoint: 71,
          ac_running: 1,
          indoor_hum: 52,
          central_cool: 0
        };

        window.fetch = async (url) => {
          if (url.includes('status.json')) {
            return {
              json: async () => oldStatus,
              text: async () => JSON.stringify(oldStatus)
            };
          } else if (url.includes('history.csv')) {
            return {
              text: async () => csvData
            };
          }
          throw new Error(`Unexpected fetch: ${url}`);
        };

        window.Chart = function() {
          return {
            destroy: () => {}
          };
        };
      }
    });

    const { window } = dom;
    const { document } = window;

    await new Promise(resolve => setTimeout(resolve, 100));

    // Should render without crashing
    const cards = document.getElementById('cards');
    assert.ok(cards, 'Should have #cards div');

    const cardElements = cards.querySelectorAll('.card');
    assert.ok(
      cardElements.length > 0,
      `Expected at least one card, got ${cardElements.length}`
    );
  });
} else {
  // ========================================================================
  // Static fallback: analyze HTML for defensive guards
  // ========================================================================

  test('HTML guards access to new status keys (static analysis)', () => {
    // These are the NEW keys that didn't exist in the old schema:
    // mismatch, cmd, confirmed, unconfirmed, applied, error
    //
    // Each access should be guarded with && or truthy checks to prevent crashes

    const newKeys = [
      { key: 'mismatch', pattern: /s\.mismatch\s*&&/ },
      { key: 'cmd', pattern: /s\.cmd\s*&&/ },
      { key: 'applied', pattern: /s\.applied/ },
      { key: 'error', pattern: /if\s*\(\s*s\.error\s*\)/ },
      { key: 'unconfirmed', pattern: /s\.unconfirmed\s*&&/ }
    ];

    for (const { key, pattern } of newKeys) {
      const hasGuard = pattern.test(htmlContent);
      assert.ok(
        hasGuard,
        `HTML should guard access to s.${key} with && or conditional check`
      );
    }
  });

  test('HTML uses descriptive placeholder for missing fields', () => {
    // Old CSV may not have all the new columns
    // Verify the code uses safe defaults (like "—" or empty strings)
    assert.ok(
      htmlContent.includes('"—"'),
      'HTML should use "—" placeholder for missing/null values'
    );
  });

  test('HTML avoids direct bracket access for new keys', () => {
    // Verify no unsafe patterns like s[newKey] without guard
    // This is a heuristic check
    const unsafePattern = /s\["?(mismatch|cmd|applied|error|unconfirmed)"?\]/;
    const hasUnsafe = unsafePattern.test(htmlContent);
    assert.equal(
      hasUnsafe,
      false,
      'HTML should not directly access new keys without guards'
    );
  });
}

test('HTML has proper fallback display elements', () => {
  // Verify that elements like #stalewarn, #cmdwarn exist
  // so warnings can be shown even if rendering is partial
  assert.ok(
    htmlContent.includes('id="stalewarn"'),
    'Should have stalewarn element'
  );
  assert.ok(
    htmlContent.includes('id="cmdwarn"'),
    'Should have cmdwarn element'
  );
  assert.ok(
    htmlContent.includes('id="cards"'),
    'Should have cards container'
  );
});

test('load function has try-catch for fetch failures', () => {
  // The load() function should handle both status.json and history.csv
  // fetch failures gracefully
  const loadFn = htmlContent.match(/async function load\(\)\s*\{[\s\S]*?\n\s*\}/)[0];
  assert.ok(
    loadFn.includes('try{') && loadFn.includes('catch(e){}'),
    'load() should have try-catch blocks around fetch calls'
  );
});
