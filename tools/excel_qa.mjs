import {createRequire} from 'node:module';
import {mkdir,writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=process.env.FRONTEND_QA_DIR,url=process.env.FRONTEND_QA_URL;
const examples=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../csvhub/web');
await mkdir(path.join(root,'downloads'),{recursive:true});
const browser=await chromium.launchPersistentContext(path.join(root,'browser-profile'),{
  executablePath:process.env.FRONTEND_QA_BROWSER,headless:true,viewport:{width:1440,height:1050},
  downloadsPath:path.join(root,'downloads'),
});
const page=await browser.newPage(),errors=[],results=[];
page.on('pageerror',error=>errors.push(error.message));
try{
  await page.goto(url);
  await page.getByRole('button',{name:'连接本地服务'}).first().click();
  await page.getByLabel('访问令牌',{exact:true}).fill(process.env.CSVHUB_API_TOKEN);
  await page.getByRole('button',{name:'连接服务',exact:true}).click();
  await page.getByText('服务已连接',{exact:true}).waitFor();
  for(const format of ['xls','xlsx']){
    const name=`Excel 验证 ${format.toUpperCase()}`;
    await page.getByRole('link',{name:'文件导入',exact:true}).click();
    await page.locator('#file-input').setInputFiles(path.join(examples,`example.${format}`));
    assert(await page.locator('#encoding').isHidden(),'CSV encoding must be hidden for Excel');
    await page.getByRole('button',{name:'校验并上传',exact:true}).click();
    await page.locator('#excel-sheet').waitFor();
    assert.equal(await page.locator('#excel-sheet option').count(),2);
    await page.locator('#excel-sheet').selectOption('备选数据');
    await page.getByText('009999',{exact:true}).waitFor({state:'attached'});
    await page.locator('details summary').click();
    await page.getByText('009999',{exact:true}).waitFor();
    await page.locator('#target-dataset').selectOption('');
    await page.locator('#dataset-name').fill(name);
    await page.locator('[data-map="2"][data-key="kind"]').selectOption('date');
    await page.locator('[data-map="3"][data-key="kind"]').selectOption('integer');
    await page.locator('[data-map="4"][data-key="kind"]').selectOption('integer');
    if(await page.locator('details:not([open]) summary').count())await page.locator('details summary').click();
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:path.join(root,`01-import-${format}.png`),fullPage:true});
    await page.getByRole('button',{name:'开始后台导入',exact:true}).click();
    await page.getByRole('heading',{name:'导入任务',exact:true}).waitFor();
    const task=page.locator('#job-list tbody tr').filter({hasText:name});
    await task.getByText('已完成',{exact:true}).waitFor({timeout:20000});
    await page.getByRole('link',{name:'数据查询',exact:true}).click();
    await page.locator('#query-dataset').selectOption({label:name});
    await page.locator('[data-filter="0"][data-key="value"]').fill('009999');
    await page.getByRole('button',{name:'查询数据',exact:true}).click();
    await page.locator('.result-table tbody tr').waitFor();
    assert.equal(await page.locator('.result-table tbody tr').count(),1);
    const row=await page.locator('.result-table tbody tr').innerText();
    assert(row.includes('009999')&&row.includes('2026-10-12')&&row.includes('备选部件')&&row.includes('18'),row);
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:path.join(root,`02-query-${format}.png`),fullPage:true});
    await page.locator('[data-filter="0"][data-key="value"]').fill('000001');
    await page.getByRole('button',{name:'查询数据',exact:true}).click();
    await page.getByRole('heading',{name:'没有找到匹配记录',exact:true}).waitFor();
    results.push({format,status:'passed',sheets:2,selected_sheet:'备选数据',imported_rows:1});
  }
  assert.equal(errors.length,0,JSON.stringify(errors));
  const result={status:'passed',formats:results,browser_errors:errors,
    checks:['Excel file selection','CSV controls hidden','worksheet switch','field types','background import','selected sheet only','index query','saved formula result']};
  await writeFile(path.join(root,'result.json'),JSON.stringify(result,null,2));
  console.log(JSON.stringify(result,null,2));
}finally{await browser.close();}
