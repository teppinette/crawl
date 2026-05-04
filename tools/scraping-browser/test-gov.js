const puppeteer = require('puppeteer-core');

const SBR_WS = 'wss://brd-customer-hl_7bf69e76-zone-scraping_browser1:fb2krsn38kp1@brd.superproxy.io:9222';

const targets = [
    { name: 'CN GSXT', url: 'https://www.gsxt.gov.cn/', country: 'cn' },
    { name: 'AE DED', url: 'https://www.dubaided.gov.ae/', country: 'ae' },
    { name: 'IN MCA', url: 'https://www.mca.gov.in/mcafoportal/viewCompanyMasterData.do', country: 'in' },
    { name: 'TR TOBB', url: 'https://www.ticaretsicil.gov.tr/', country: 'tr' },
];

// Test one target at a time
const target = targets[parseInt(process.argv[2] || '0')];

(async () => {
    console.log(`Testing: ${target.name} — ${target.url}`);
    let browser;
    try {
        browser = await puppeteer.connect({
            browserWSEndpoint: SBR_WS,
        });
        const page = await browser.newPage();
        // Scraping Browser manages headers automatically

        const response = await page.goto(target.url, { waitUntil: 'domcontentloaded', timeout: 60000 });

        console.log(`Status: ${response.status()}`);

        const title = await page.title();
        console.log(`Title: ${title}`);

        const bodyLen = await page.evaluate(() => document.body.innerHTML.length);
        console.log(`Body: ${bodyLen} chars`);

        // Get text content for verification
        const text = await page.evaluate(() => document.body.innerText.substring(0, 500));
        console.log(`Content: ${text.substring(0, 300)}`);

        console.log(`\nRESULT: ${target.name} — ${response.status() === 200 ? 'SUCCESS' : 'FAILED'}`);
    } catch (e) {
        console.log(`ERROR: ${e.message}`);
    } finally {
        if (browser) await browser.close();
    }
})();
