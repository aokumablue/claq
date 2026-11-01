const { chromium } = require('playwright');
(async () => {
  const dir = process.argv[2];
  const browser = await chromium.launch();
  for (const page_ of ['index.html', 'empty.html']) {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    await page.goto('file://' + dir + '/' + page_);
    await page.evaluate(() => localStorage.setItem('x', 'y'));
    const btn = page.locator('[data-base-value="zinc"]');
    if (await btn.count()) await btn.first().click({ force: true });
    const base = await page.evaluate(() => document.documentElement.dataset.base);
    const tools = await page.locator('.list-tools').count();
    const counts = await page.locator('.list-tools').allTextContents();
    await page.reload();
    const baseAfter = await page.evaluate(() => document.documentElement.dataset.base);
    console.log(page_, JSON.stringify({ errors, base, baseAfter, tools }));
  }
  await browser.close();
})();
