// Capture the mobile view after the navigation transition has finished.
import {createRequire} from 'node:module';
import path from 'node:path';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=process.env.FRONTEND_QA_DIR;
const browser=await chromium.launchPersistentContext(path.join(root,'preview-profile'),{executablePath:process.env.FRONTEND_QA_BROWSER,headless:true,viewport:{width:390,height:844}});
try{
  const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.FRONTEND_QA_URL);await page.getByRole('button',{name:'展开导航'}).click();await page.locator('.account').click();
  await page.locator('#login-username').fill('worker-alpha');await page.locator('#login-password').fill('Synthetic-pass-123');await page.locator('#account-login-form button[type=submit]').click();await page.getByText('服务已连接',{exact:true}).waitFor();
  await page.locator('[data-nav=extract]').click();await page.waitForFunction(()=>document.querySelector('.sidebar').getBoundingClientRect().right<=1);
  await page.locator('#extract-dataset').selectOption({label:'库存提取演示'});await page.locator('.extract-table').waitFor();
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.waitForFunction(()=>document.querySelectorAll('.toast').length===0);
  await page.screenshot({path:path.join(root,'04-extraction-mobile.png'),fullPage:true});
  assert.deepEqual(errors,[]);console.log(JSON.stringify({mobile_preview:'passed',browser_errors:errors}));
}finally{await browser.close();}
