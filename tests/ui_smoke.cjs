// Drives the real console in Chrome. Run via tests/ui_smoke.py, which starts the
// server and passes SMOKE_BASE / SMOKE_TOKEN.
const puppeteer = require('puppeteer-core');

const BASE = process.env.SMOKE_BASE;
const TOKEN = process.env.SMOKE_TOKEN;
const CHROME = process.env.SMOKE_CHROME || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

const problems = [];
function check(name, ok, detail) {
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? ' :: ' + detail : ''));
  if (!ok) problems.push(name);
}

(async () => {
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1400, height: 900 });
  page.on('pageerror', (e) => { console.log('PAGEERROR ' + e.message); problems.push('pageerror'); });
  page.on('console', (m) => { if (m.type() === 'error') console.log('CONSOLE ' + m.text().slice(0, 200)); });

  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('#token', { timeout: 15000 });
  await page.type('#token', TOKEN);
  await page.keyboard.press('Enter');
  await page.waitForSelector('#app:not([hidden])', { timeout: 15000 });

  const navLabels = await page.$$eval('#nav button', (bs) => bs.map((b) => b.textContent.trim()));
  check('nav has AI 权限', navLabels.includes('AI 权限'), navLabels.join('|'));
  check('nav has 凭证库', navLabels.includes('凭证库'));

  // ---- AI 权限
  await page.evaluate(() => {
    const b = Array.from(document.querySelectorAll('#nav button')).find((x) => x.textContent.trim() === 'AI 权限');
    b.click();
  });
  await page.waitForFunction(() => document.body.innerText.includes('模式：决定 AI 能走多远'), { timeout: 15000 });
  await new Promise((r) => setTimeout(r, 800));

  const modeCards = await page.$$eval('.mode-card', (cs) => cs.map((c) => c.innerText));
  check('three mode cards', modeCards.length === 3, 'count=' + modeCards.length);
  const readonlyCard = modeCards.find((t) => t.includes('只读排查')) || '';
  check('readonly card hides write list', !readonlyCard.includes('写名单'), readonlyCard.replace(/\s+/g, ' ').slice(0, 120));
  check('readonly card shows read-only list', readonlyCard.includes('只读名单'));
  const confirmCard = modeCards.find((t) => t.includes('提议')) || '';
  check('confirm card shows write list', confirmCard.includes('写名单'));

  const panels = await page.$$eval('.list-panel h3', (hs) => hs.map((h) => h.textContent.trim()));
  check('three list panels', panels.length === 3, panels.join('|'));

  const rules = await page.$$eval('.rule-row', (rs) => rs.length);
  check('rules rendered', rules > 0, 'rows=' + rules);
  const hasToggle = await page.$$eval('.rule-row input[type=checkbox]', (xs) => xs.length);
  check('every rule has a toggle', hasToggle === rules, hasToggle + '/' + rules);
  const hasDots = await page.$$eval('.rule-row button', (xs) => xs.filter((b) => b.textContent.trim() === '⋯').length);
  check('every rule has an expand button', hasDots === rules, hasDots + '/' + rules);

  // toggle a rule off, validate, and confirm the empty-name check fires
  await page.click('.rule-row input[type=checkbox]');
  await page.evaluate(() => {
    const b = Array.from(document.querySelectorAll('button')).find((x) => x.textContent.trim() === '校验');
    b.click();
  });
  await new Promise((r) => setTimeout(r, 600));
  const warn = await page.$eval('.card.warn', (n) => n.innerText).catch(() => '');
  check('validation panel appears', warn.length > 0, warn.replace(/\s+/g, ' ').slice(0, 100));

  // add a rule, leave it blank, expect validation to block the save
  await page.evaluate(() => {
    const b = Array.from(document.querySelectorAll('button')).find((x) => x.textContent.trim().includes('添加规则'));
    b.click();
  });
  await new Promise((r) => setTimeout(r, 400));
  await page.evaluate(() => {
    const b = Array.from(document.querySelectorAll('button')).find((x) => x.textContent.trim() === '校验');
    b.click();
  });
  await new Promise((r) => setTimeout(r, 600));
  const warn2 = await page.$eval('.card.warn', (n) => n.innerText).catch(() => '');
  check('blank rule name is rejected', warn2.includes('规则名不能为空'), warn2.replace(/\s+/g, ' ').slice(0, 140));

  await page.setViewport({ width: 1400, height: 2600 });
  await new Promise((r) => setTimeout(r, 500));
  await page.screenshot({ path: 'ui_aiperm.png' });

  // ---- 凭证库
  await page.evaluate(() => {
    const b = Array.from(document.querySelectorAll('#nav button')).find((x) => x.textContent.trim() === '凭证库');
    b.click();
  });
  await page.waitForFunction(() => document.body.innerText.includes('凭证库'), { timeout: 15000 });
  await new Promise((r) => setTimeout(r, 800));
  const vaultText = await page.evaluate(() => document.getElementById('console-body').innerText);
  check('vault lists the asset', vaultText.includes('演示 Web'), vaultText.replace(/\s+/g, ' ').slice(0, 120));
  check('vault states values stay on host', vaultText.includes('值不在主服务里'), '');
  await page.screenshot({ path: 'ui_vault.png', fullPage: true });

  await browser.close();
  console.log(problems.length ? 'SMOKE FAILED: ' + problems.join(', ') : 'SMOKE OK');
  process.exit(problems.length ? 1 : 0);
})().catch((e) => { console.log('FATAL ' + e.message); process.exit(1); });
