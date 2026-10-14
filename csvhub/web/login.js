const $=selector=>document.querySelector(selector);
const forms=[$('#account-login-form'),$('#token-login-form')];
let busy=false;
const messages={
  'Incorrect username or password':'用户名或密码不正确。',
  'Login required or session expired':'令牌无效或登录已失效。',
  'Too many login attempts; retry in one minute':'登录次数过多，请一分钟后重试。',
  'Login capacity is busy; retry shortly':'服务正在处理中，请稍后重试。'
};
function message(text,error=false){const el=$('#login-message');el.textContent=text;el.classList.toggle('error-text',error);}
function setBusy(value){busy=value;for(const form of forms)for(const input of form.elements)input.disabled=value;}
async function request(path,options={}){
  let response;
  try{response=await fetch('/api/v1'+path,{...options,cache:'no-store'});}catch{throw new Error('无法连接服务，请稍后重试。');}
  let data;
  try{data=await response.json();}catch{throw new Error('服务响应异常，请稍后重试。');}
  if(!response.ok){const error=new Error(messages[data.detail]||(response.status===422?'请检查用户名和密码格式。':'登录失败，请稍后重试。'));error.status=response.status;throw error;}
  return data;
}
async function verify(token){return request('/auth/me',{headers:{Authorization:'Bearer '+token}});}
function enterWorkspace(token){sessionStorage.setItem('csvhub.token',token);$('#login-password').value='';$('#login-token').value='';location.replace('/workspace#overview');}
async function restoreSession(){
  const token=sessionStorage.getItem('csvhub.token');
  if(!token)return;
  setBusy(true);message('正在验证登录…');
  try{await verify(token);enterWorkspace(token);}catch(error){
    if(error.status===401){sessionStorage.removeItem('csvhub.token');sessionStorage.removeItem('csvhub.upload');}
    message(error.status===401?'登录已失效，请重新登录。':error.message,true);setBusy(false);
  }
}
document.addEventListener('submit',async event=>{
  if(!forms.includes(event.target))return;
  event.preventDefault();if(busy)return;
  const form=event.target;
  // Read before disabling the fields; only the server may establish the identity.
  const username=$('#login-username').value.trim(),password=$('#login-password').value,token=$('#login-token').value.trim();
  setBusy(true);message('正在登录…');
  try{
    let candidate;
    if(form.id==='account-login-form'){
      const data=await request('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});
      if(typeof data.token!=='string'||!data.token)throw new Error('服务响应异常，请稍后重试。');
      candidate=data.token;
    }else candidate=token;
    await verify(candidate);sessionStorage.removeItem('csvhub.upload');enterWorkspace(candidate);
  }catch(error){message(error.message,true);setBusy(false);}
});
window.addEventListener('pageshow',event=>{if(event.persisted){$('#login-password').value='';$('#login-token').value='';setBusy(false);restoreSession();}});
const notice=sessionStorage.getItem('csvhub.login-message');
sessionStorage.removeItem('csvhub.login-message');
if(notice)message(notice);
restoreSession();
