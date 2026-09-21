#!/usr/bin/env node
// Copy one tab of the Lab Tracker workbook out of the already-running Chrome
// (CDP on http://localhost:29229) and write it to disk as TSV + HTML.
//
// Raw CDP over Node's built-in WebSocket (Node >= 22): no packages to install.
// The sheet is opened anonymously via its share link, so the Sheets API is not
// an option; select-all + copy and reading the clipboard is the working route.
// The HTML copy is kept because HYPERLINK cells copy as display text in TSV —
// the real hrefs only exist in the text/html clipboard item.
//
// Usage: node read_sheet.mjs "Lab Tracker" [--out DIR] [--url SHEET_URL]
//   writes DIR/sheet_Lab_Tracker.tsv and DIR/sheet_Lab_Tracker.html

import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';

const DEFAULT_URL =
  'https://docs.google.com/spreadsheets/d/1quwdwnA-hbyQ3XegaBSvljwp96QiaqhAQQ1A076tjoU/edit?gid=1253025117#gid=1253025117';
const CDP = process.env.CDP_URL ?? 'http://localhost:29229';

const args = process.argv.slice(2);
const tab = args.find((a) => !a.startsWith('--')) ?? 'Lab Tracker';
const opt = (name, dflt) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : dflt;
};
const outDir = opt('--out', '.');
const sheetUrl = opt('--url', DEFAULT_URL);
const sheetId = sheetUrl.match(/\/spreadsheets\/d\/([^/]+)/)?.[1];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Minimal Chrome DevTools Protocol client over a single WebSocket. */
class Cdp {
  /** @param {WebSocket} ws */
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) {
          reject(new Error(`${msg.error.message} (${msg.error.data ?? ''})`));
        } else {
          resolve(msg.result);
        }
      }
    });
  }
  /**
   * @param {string} url The browser's webSocketDebuggerUrl.
   * @return {!Promise<!Cdp>}
   */
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', rej, { once: true });
    });
    return new Cdp(ws);
  }
  /**
   * @param {string} method
   * @param {!Object=} params
   * @param {string=} sessionId Target session for flattened page commands.
   * @return {!Promise<*>} The command result.
   */
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params, sessionId }));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
  }
  close() {
    this.ws.close();
  }
}

async function main() {
  const version = await (await fetch(`${CDP}/json/version`)).json();
  const browser = await Cdp.connect(version.webSocketDebuggerUrl);

  // Reuse a tab that already shows this workbook, otherwise open a new one.
  const { targetInfos } = await browser.send('Target.getTargets');
  let target = targetInfos.find(
    (t) => t.type === 'page' && sheetId && t.url.includes(sheetId),
  );
  if (!target) {
    const { targetId } = await browser.send('Target.createTarget', { url: sheetUrl });
    target = { targetId };
  }
  const { sessionId } = await browser.send('Target.attachToTarget', {
    targetId: target.targetId,
    flatten: true,
  });
  const page = (method, params) => browser.send(method, params, sessionId);

  await page('Page.enable');
  await page('Runtime.enable');
  await page('Page.bringToFront');
  await browser.send('Browser.grantPermissions', {
    origin: 'https://docs.google.com',
    permissions: ['clipboardReadWrite', 'clipboardSanitizedWrite'],
  });

  const evaluate = async (expression, awaitPromise = false) => {
    const { result, exceptionDetails } = await page('Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise,
    });
    if (exceptionDetails) {
      throw new Error(
        `${exceptionDetails.text} ${JSON.stringify(exceptionDetails.exception)}`,
      );
    }
    return result.value;
  };

  // Wait for the grid + name box to exist (anonymous load takes a few seconds).
  for (let i = 0; i < 40; i++) {
    const ready = await evaluate(
      `!!document.querySelector('#t-name-box') &&
       document.querySelectorAll('.docs-sheet-tab').length > 0`,
    );
    if (ready) break;
    await sleep(500);
  }

  // The tab element's textContent carries extra text, so match by suffix.
  const clicked = await evaluate(`(() => {
    const el = [...document.querySelectorAll('.docs-sheet-tab')]
      .find((e) => e.textContent.trim().endsWith(${JSON.stringify(tab)}));
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return [r.x + r.width / 2, r.y + r.height / 2];
  })()`);
  if (!clicked) throw new Error(`tab "${tab}" not found`);
  await mouseClick(page, clicked[0], clicked[1]);
  await sleep(1200);

  // Name box -> A1 -> Enter puts focus back on the grid at A1.
  const nb = await evaluate(`(() => {
    const r = document.querySelector('#t-name-box').getBoundingClientRect();
    return [r.x + r.width / 2, r.y + r.height / 2];
  })()`);
  await mouseClick(page, nb[0], nb[1]);
  await selectAllAndType(page, 'A1');
  await key(page, 'Enter', 13);
  await sleep(400);
  await key(page, 'a', 65, 2); // Ctrl+A
  await sleep(300);
  await key(page, 'c', 67, 2); // Ctrl+C
  await sleep(1500);

  const clip = await evaluate(
    `(async () => {
      const items = await navigator.clipboard.read();
      const out = {};
      for (const it of items) {
        for (const t of it.types) out[t] = await (await it.getType(t)).text();
      }
      return out;
    })()`,
    true,
  );
  const tsv = clip['text/plain'] ?? '';
  const html = clip['text/html'] ?? '';
  if (!tsv.trim()) throw new Error('clipboard came back empty — is the sheet focused?');

  mkdirSync(outDir, { recursive: true });
  const base = join(outDir, `sheet_${tab.replace(/[^A-Za-z0-9]+/g, '_')}`);
  writeFileSync(`${base}.tsv`, tsv);
  writeFileSync(`${base}.html`, html);
  const rows = tsv
    .split('\n')
    .filter((l) => l.split('\t').some((c) => c.trim())).length;
  console.log(`${tab}: ${rows} non-empty rows -> ${base}.tsv / .html`);
  browser.close();
}

/**
 * @param {function(string, !Object=): !Promise<*>} page
 * @param {number} x
 * @param {number} y
 */
async function mouseClick(page, x, y) {
  for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) {
    await page('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1 });
  }
}

/**
 * Presses and releases one key.
 * @param {function(string, !Object=): !Promise<*>} page
 * @param {string} keyName DOM key value, e.g. 'a' or 'Enter'.
 * @param {number} code Windows virtual key code.
 * @param {number=} modifiers CDP modifier bitmask (2 = Ctrl).
 */
async function key(page, keyName, code, modifiers = 0) {
  const isChar = keyName.length === 1;
  const base = {
    key: keyName,
    code: isChar ? `Key${keyName.toUpperCase()}` : keyName,
    windowsVirtualKeyCode: code,
    modifiers,
  };
  const text = isChar && !modifiers ? keyName : undefined;
  await page('Input.dispatchKeyEvent', { type: 'keyDown', ...base, text });
  await page('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
}

/**
 * @param {function(string, !Object=): !Promise<*>} page
 * @param {string} text
 */
async function selectAllAndType(page, text) {
  await key(page, 'a', 65, 2);
  await page('Input.insertText', { text });
}

main().catch((e) => {
  console.error(e.message);
  process.exit(1);
});
