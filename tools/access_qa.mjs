import {createRequire} from 'node:module';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import path from 'node:path';
import assert from 'node:assert/strict';
const require=createRequire(import.meta.url),{chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=process.env.FRONTEND_QA_DIR,url=process.env.FRONTEND_QA_URL,token=process.env.CSVHUB_API_TOKEN;
const browser=await chromium.launchPersistentContext(path.join(root,'browser-profile'),{executablePath:process.env.FRONTEND_QA_BROWSER,headless:true,viewport:{width:1440,height:1050}});
const page=await browser.newPage(),errors=[],checks=[];
page.on('pageerror',e=>errors.push(e.message));
const adminHeaders={Authorization:'Bearer '+token};
async function call(method,route,data,headers=adminHeaders){const r=await page.request.fetch(url+'/api/v1'+route,{method,headers,...(data!==undefined?{data}:{})});assert(r.ok(),await r.text());return r.json();}
async function accountLogin(username,password='Synthetic-pass-123'){await page.locator('.account').click();await page.locator('#login-username').fill(username);await page.locator('#login-password').fill(password);await page.locator('#account-login-form button[type=submit]').click();await page.getByText('服务已连接',{exact:true}).waitFor();await page.locator('#dialog').waitFor({state:'hidden'});}
async function logout(){await page.locator('#account-exit').click();await page.waitForFunction(()=>document.querySelector('#connection-state').textContent==='未连接');}
try{
  await page.goto(url);
  // Prepare a generic synthetic inventory through the same streaming import API.
  const content=await readFile(process.env.FRONTEND_QA_FILE),sha=createHash('sha256').update(content).digest('hex');
  const upload=await call('POST','/uploads',{filename:'inventory-ui.csv',size:content.length,sha256:sha});
  for(let offset=0;offset<content.length;offset+=131072){const chunk=content.subarray(offset,offset+131072);await call('PUT',`/uploads/${upload.id}/chunks?offset=${offset}`,chunk,{...adminHeaders,'X-Chunk-SHA256':createHash('sha256').update(chunk).digest('hex'),'Content-Type':'application/octet-stream'});}
  await call('POST',`/uploads/${upload.id}/complete`);
  const dataset=await call('POST','/datasets',{name:'库存提取演示',columns:[{name:'编号',source:'record_key'},{name:'名称',source:'label'},{name:'日期',source:'event_date',kind:'date'},{name:'数量',source:'quantity',kind:'integer'}]});
  const job=await call('POST',`/datasets/${dataset.id}/imports`,{upload_id:upload.id});
  for(let i=0;i<200;i++){const status=await call('GET','/jobs/'+job.id);if(status.status==='completed')break;if(status.status==='failed')throw Error(status.error);await page.waitForTimeout(100);}
  assert.equal((await call('GET','/jobs/'+job.id)).status,'completed');
  await page.getByRole('button',{name:'连接本地服务',exact:true}).first().click();await page.getByLabel('访问令牌',{exact:true}).fill(token);await page.getByRole('button',{name:'连接服务',exact:true}).click();await page.getByText('服务已连接',{exact:true}).waitFor();
  await page.getByRole('link',{name:'系统设置',exact:true}).click();await page.locator('#user-management').waitFor();
  for(const username of ['worker-alpha','worker-beta']){await page.locator('#new-username').fill(username);await page.locator('#new-password').fill('Synthetic-pass-123');await page.getByRole('button',{name:'创建用户',exact:true}).click();await page.locator('#user-management tbody').getByText(username,{exact:true}).waitFor();}
  checks.push('administrator creates custom accounts in UI');
  await page.getByRole('button',{name:'设置管理员账号'}).click();await page.locator('#edit-username').fill('manager');await page.locator('#edit-password').fill('Synthetic-pass-123');await page.getByRole('button',{name:'保存账号'}).click();await page.locator('#user-management tbody').getByText('manager',{exact:true}).waitFor();
  await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:path.join(root,'01-user-management.png'),fullPage:true});
  await logout();await accountLogin('manager');await page.getByRole('link',{name:'系统设置',exact:true}).waitFor();checks.push('administrator password setup and password login');
  await logout();await accountLogin('worker-alpha');
  await page.getByRole('heading',{name:'工作台概览',exact:true}).waitFor();
  assert.deepEqual(await page.locator('[data-nav]:visible').allTextContents(),['工作台概览','数据提取','数据查询']);
  await page.evaluate(()=>location.hash='settings');await page.getByRole('heading',{name:'工作台概览',exact:true}).waitFor();assert.equal(await page.locator('#user-management').count(),0);
  const userToken=await page.evaluate(()=>sessionStorage.getItem('csvhub.token'));
  for(const route of ['/users','/jobs','/stats']){const response=await page.request.get(url+'/api/v1'+route,{headers:{Authorization:'Bearer '+userToken}});assert.equal(response.status(),403);}
  checks.push('ordinary user sees exactly three menus and server rejects administration');
  await page.screenshot({path:path.join(root,'02-user-overview.png'),fullPage:true});
  await page.locator('[data-nav=extract]').click();await page.locator('#extract-dataset').selectOption(dataset.id);await page.getByRole('button',{name:'提取数据',exact:true}).waitFor({state:'visible'});await page.getByRole('button',{name:'提取数据',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.extract-table tbody tr').length===100);
  assert.equal(await page.locator('#extract-size option').count(),10);assert.equal(await page.locator('.extract-table tbody tr').first().locator('td').first().textContent(),'1');
  assert.equal(await page.locator('.extract-table img').count(),0);assert.equal(await page.evaluate(()=>window.__xss),undefined);
  checks.push('100-record ordered batch and safe cell rendering');
  const mark=page.getByRole('button',{name:'标注记录 1',exact:true});await mark.click();await page.waitForFunction(()=>document.querySelector('[data-extract-row="1"] .mark-button').textContent==='已标注');
  const note=page.getByLabel('记录 1 备注',{exact:true});await note.fill('<img src=x onerror="window.__notes=1"> 已检查');await note.press('Tab');await page.waitForFunction(()=>document.querySelector('[data-note-status="1"]').textContent==='已保存');
  await page.screenshot({path:path.join(root,'03-extraction.png'),fullPage:true});
  await page.reload();await page.getByText('服务已连接',{exact:true}).waitFor();await page.locator('#extract-dataset').selectOption(dataset.id);await page.waitForFunction(()=>document.querySelector('[data-extract-row="1"] .mark-button')?.textContent==='已标注');assert((await page.getByLabel('记录 1 备注',{exact:true}).inputValue()).includes('已检查'));assert.equal(await page.evaluate(()=>window.__notes),undefined);
  await page.getByRole('button',{name:'标注记录 1',exact:true}).click();await page.waitForFunction(()=>document.querySelector('[data-extract-row="1"] .mark-button').textContent==='未标注');
  checks.push('mark, unmark and notes persist after reload');
  await page.locator('#extract-size').selectOption('1000');await page.getByRole('button',{name:'继续提取下一批'}).click();await page.waitForFunction(()=>document.querySelectorAll('.extract-table tbody tr').length===1000);assert.equal(await page.locator('.extract-table tbody tr').first().locator('td').first().textContent(),'101');
  const history=await page.locator('#extract-history option').nth(2).getAttribute('value');await page.locator('#extract-history').selectOption(history);await page.waitForFunction(()=>document.querySelectorAll('.extract-table tbody tr').length===100);assert((await page.getByLabel('记录 1 备注',{exact:true}).inputValue()).includes('已检查'));
  checks.push('1000-record next batch and history restoration');
  await page.getByRole('button',{name:'标注记录 3',exact:true}).click();await page.waitForFunction(()=>document.querySelector('[data-extract-row="3"] .mark-button').textContent==='已标注');
  let releaseNote,notifyNote;const noteReleased=new Promise(r=>releaseNote=r),noteBlocked=new Promise(r=>notifyNote=r);
  await page.route('**/api/v1/extractions/*/rows/2',async route=>{const response=await route.fetch();notifyNote();await noteReleased;await route.fulfill({response});});
  await page.getByLabel('记录 2 备注',{exact:true}).fill('切换页面时继续保存');await page.getByLabel('记录 2 备注',{exact:true}).press('Tab');await noteBlocked;
  await page.getByRole('link',{name:'数据查询',exact:true}).click();releaseNote();await page.waitForTimeout(200);await page.unroute('**/api/v1/extractions/*/rows/2');
  assert.deepEqual(errors,[]);checks.push('in-flight note save survives navigation away from the batch');
  await page.locator('#query-dataset').selectOption(dataset.id);await page.getByRole('button',{name:'查询数据',exact:true}).click();await page.locator('.result-table tbody tr').first().waitFor();assert.equal(await page.locator('.result-table tbody tr').count(),50);checks.push('ordinary user can query published data');
  assert.equal(await page.locator('.result-table tbody tr').nth(0).locator('.query-mark').textContent(),'未标注');
  assert((await page.locator('.result-table tbody tr').nth(0).locator('.query-note').textContent()).includes('已检查'));
  assert.equal(await page.locator('.result-table tbody tr').nth(1).locator('.query-note').textContent(),'切换页面时继续保存');
  assert.equal(await page.locator('.result-table tbody tr').nth(2).locator('.query-mark').textContent(),'已标注');
  assert.equal(await page.locator('.result-table img').count(),0);assert.equal(await page.evaluate(()=>window.__notes),undefined);
  checks.push('query displays durable marks and notes with escaped content');
  await page.screenshot({path:path.join(root,'05-query-annotations.png'),fullPage:true});
  await page.reload();await page.getByText('服务已连接',{exact:true}).waitFor();await page.locator('#query-dataset').selectOption(dataset.id);await page.getByRole('button',{name:'查询数据',exact:true}).click();await page.locator('.result-table tbody tr').first().waitFor();
  assert.equal(await page.locator('.result-table tbody tr').nth(2).locator('.query-mark').textContent(),'已标注');assert((await page.locator('.result-table tbody tr').nth(0).locator('.query-note').textContent()).includes('已检查'));
  checks.push('query marks and notes survive a full browser reload');
  await page.locator('[data-nav=extract]').click();await page.locator('#extract-dataset').selectOption(dataset.id);await page.locator('.extract-table').waitFor();const earlier=(await page.locator('#extract-history option').nth(2).getAttribute('value'));await page.locator('#extract-history').selectOption(earlier);await page.getByRole('button',{name:'标注记录 3',exact:true}).click();await page.waitForFunction(()=>document.querySelector('[data-extract-row="3"] .mark-button').textContent==='未标注');await page.getByLabel('记录 3 备注',{exact:true}).fill('修改后备注');await page.getByLabel('记录 3 备注',{exact:true}).press('Tab');await page.waitForFunction(()=>document.querySelector('[data-note-status="3"]').textContent==='已保存');
  await page.locator('[data-nav=query]').click();assert.equal(await page.locator('.result-table tbody tr').nth(2).locator('.query-mark').textContent(),'未标注');assert.equal(await page.locator('.result-table tbody tr').nth(2).locator('.query-note').textContent(),'修改后备注');
  const refreshed=page.waitForResponse(r=>r.url().endsWith('/query')&&r.request().method()==='POST');await page.getByRole('button',{name:'查询数据',exact:true}).click();const refreshedData=await (await refreshed).json();assert(refreshedData.cache_hit);assert.equal(refreshedData.annotations['3'].note,'修改后备注');
  checks.push('changing marks updates displayed results and cached queries read fresh notes');
  await logout();await accountLogin('worker-beta');await page.locator('[data-nav=extract]').click();await page.locator('#extract-dataset').selectOption(dataset.id);await page.getByRole('button',{name:'提取数据',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.extract-table tbody tr').length===100);assert.equal(await page.locator('.extract-table tbody tr').first().locator('td').first().textContent(),'1');assert.equal(await page.getByLabel('记录 1 备注',{exact:true}).inputValue(),'');
  checks.push('users have independent extraction positions and annotations');
  await page.locator('[data-nav=query]').click();await page.locator('#query-dataset').selectOption(dataset.id);await page.getByRole('button',{name:'查询数据',exact:true}).click();await page.locator('.result-table tbody tr').first().waitFor();assert.equal(await page.locator('.result-table tbody tr').nth(2).locator('.query-note').textContent(),'—');assert.equal(await page.locator('.result-table tbody tr').nth(2).locator('.query-mark').textContent(),'未标注');checks.push('cached source pages do not leak another user annotation');await page.locator('[data-nav=extract]').click();
  await page.setViewportSize({width:390,height:844});await page.waitForFunction(()=>document.querySelector('.sidebar').getBoundingClientRect().right<=1);await page.screenshot({path:path.join(root,'04-extraction-mobile.png'),fullPage:true});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.getByRole('button',{name:'展开导航'}).click();assert.deepEqual(await page.locator('[data-nav]:visible').allTextContents(),['工作台概览','数据提取','数据查询']);await page.getByRole('link',{name:'工作台概览',exact:true}).click();checks.push('mobile extraction and limited navigation');
  const beta=(await call('GET','/users')).items.find(u=>u.username==='worker-beta');await call('PATCH','/users/'+beta.id,{enabled:false});await page.reload();await page.getByRole('dialog').waitFor();await page.waitForFunction(()=>document.querySelector('#connection-state').textContent==='未连接');checks.push('disabled account immediately loses its active session');
  assert.deepEqual(errors,[]);
  const result={status:'passed',checks,browser_errors:errors};await writeFile(path.join(root,'result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
}finally{await browser.close();}
