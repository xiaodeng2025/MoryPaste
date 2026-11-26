'use strict';
const byId=id=>document.getElementById(id);
let nativeState=null,settingsState=null,qrTimer=null,qrDeadline=0,pairStartedAt=0,pendingRevoke=null,toastTimer=null;
let activePage='home',desktopVisible=!window.chrome?.webview,receiverState=null,legacyReceipts=[];
const pendingRefresh=new Set();
let refreshWork=null;
const icon=name=>{const s=document.createElementNS('http://www.w3.org/2000/svg','svg');const u=document.createElementNS(s.namespaceURI,'use');u.setAttribute('href','#i-'+name);s.append(u);return s;};
const text=(tag,value,cls)=>{const n=document.createElement(tag);n.textContent=value;if(cls)n.className=cls;return n;};
const date=value=>{if(!value)return '时间未记录';const d=new Date(value);return Number.isNaN(+d)?'时间未记录':d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});};
const bytes=n=>Number.isFinite(n)?(n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(1)+' MB'):'大小未记录';
function toast(message,error=false){clearTimeout(toastTimer);const n=byId('toast');n.textContent=message;n.className=error?'error':'';n.hidden=false;toastTimer=setTimeout(()=>n.hidden=true,4500);}
window.navigate=page=>{if(!byId('page-'+page))return;activePage=page;document.querySelectorAll('.page').forEach(n=>n.classList.toggle('active',n.id==='page-'+page));document.querySelectorAll('.nav').forEach(n=>{n.classList.toggle('active',n.dataset.page===page);if(n.dataset.page===page)n.setAttribute('aria-current','page');else n.removeAttribute('aria-current');});document.querySelector('main').scrollTop=0;refresh();};
document.querySelectorAll('[data-page]').forEach(n=>n.onclick=()=>navigate(n.dataset.page));
async function request(path,body){const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store',signal:AbortSignal.timeout(5000)});const value=await response.json();if(!response.ok)throw Error(value.error||'请求未成功');return value;}
async function revokeAndConfirm(deviceId, api=request){
let result;
try { result=await api('/api/revoke',{deviceId}); }
catch(error){
  let state;
  try { state=await api('/api/state'); }
  catch { throw Error('无法连接接收服务，暂不能确认撤销结果。请恢复连接后重试。'); }
  if(!state.devices.some(device=>device.id===deviceId))return {status:'confirmed_absent'};
  throw Error(error instanceof TypeError || error.name==='TimeoutError' || error.name==='AbortError' ? '撤销结果未确认，设备仍在授权列表中，请重试。' : error.message);
}
return result;
}
async function bridge(method,...args){if(!window.pywebview?.api)throw Error('此功能请在 MoryPaste 桌面客户端中使用');return window.pywebview.api[method](...args);}
function closeDialog(d){d.close();if(d.id==='pairDialog')resetPairDialog();if(d.id==='confirmDialog')pendingRevoke=null;}
window.stcqEscape=()=>{const opened=[...document.querySelectorAll('dialog[open]')];if(opened.length)closeDialog(opened.at(-1));else bridge('hide_window').catch(()=>{});};
document.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();window.stcqEscape();}},true);
document.querySelectorAll('dialog').forEach(d=>d.addEventListener('cancel',e=>{e.preventDefault();closeDialog(d);}));
document.querySelector('.close-dialog').onclick=()=>closeDialog(byId('pairDialog'));
function renderDevices(devices) {
  const root = byId('deviceList');
  const signature = JSON.stringify(devices);
  if (root.dataset.signature === signature) return;
  root.dataset.signature = signature;
  root.replaceChildren();
  if (!devices.length) {
    root.append(text('div', '还没有授权手机', 'empty'));
    return;
  }
  for (const device of devices) {
    const row = text('div', '', 'device-row');
    const badge = text('div', '', 'device-icon');
    badge.append(icon('device'));
    const info = text('div', '', 'device-info');
    info.append(text('strong', device.name), text('p', '配对时间：' + date(device.created)));
    const revoke = text('button', '撤销授权', 'text-button');
    revoke.onclick = () => {
      pendingRevoke = device;
      byId('confirmMessage').textContent = '撤销“' + device.name + '”后，这台手机将无法发送文件、图片或剪贴板文字，也无法接收电脑剪贴板。你可以重新配对。';
      byId('confirmDialog').showModal();
    };
    const syncStatus = device.clipboardSync
      ? (device.clipboardConnected ? '电脑 → 手机 · 已连接，复制文字即可同步' : '电脑 → 手机 · 已授权，当前未连接')
      : '电脑 → 手机 · 未开启';
    info.append(text('p', syncStatus));
    const sync = text('button', device.clipboardSync ? '关闭同步' : '开启同步', 'text-button');
    sync.onclick = async () => {
      sync.disabled = true;
      try {
        await request('/api/clipboard-target', {deviceId: device.clipboardSync ? null : device.id});
        await queueRefresh('devices');
      } catch (error) {
        toast(error.message, true);
      } finally {
        sync.disabled = false;
      }
    };
    row.append(badge, info, sync, revoke);
    root.append(row);
  }
}

function showHomeDevices(expanded) {
  byId('homeDevices').setAttribute('aria-expanded',String(expanded));
  byId('homeDevicePanel').hidden=!expanded;
}
byId('homeDevices').onclick=()=>showHomeDevices(byId('homeDevicePanel').hidden);

let pairingAuthorization = false, lanPairError = '';
function renderHomeActions(deviceCount, running) {
  const firewall = nativeState?.firewall;
  const waiting = nativeState?.serviceState === 'WAITING';
  const needsAttention = running && firewall?.available !== false && firewall?.authorized !== true;
  const deviceButton = byId('homeDevices');
  deviceButton.hidden = deviceCount === 0;
  if (deviceButton.hidden) showHomeDevices(false);
  const canAdd = waiting || needsAttention || (running && deviceCount === 0);
  byId('homePair').hidden = !canAdd;
  byId('homeFirstSteps').hidden = !canAdd;
  byId('homeGuide').hidden = !running || needsAttention || deviceCount === 0;
  const hint = byId('lanPairHint');
  hint.hidden = !lanPairError && !waiting && !needsAttention && !pairingAuthorization;
  hint.textContent = pairingAuthorization ? '正在准备局域网接收…' : lanPairError ||
    (firewall?.authorized === true ? '添加手机时将启动局域网接收。' : '首次连接手机可能需要 Windows 授权。');
  if (needsAttention) {
    byId('serviceStatus').textContent = firewall?.authorized === false
      ? '请检查局域网授权' : '正在确认局域网授权…';
    byId('serviceDot').className = 'dot waiting';
  }
}

const receiveCategory = {image:'张图片', video:'段视频', document:'份文档', other:'个文件'};
async function openReceiveRoot() {
  if (window.pywebview?.api) {
    const result = await bridge('open_received_folder');
    if (!result.ok) throw Error(result.error);
    return;
  }
  await request('/api/received/action', {action:'openRoot'});
}
function receiptName(name) {
  const title = text('strong', '', 'receipt-name');
  const parts = /^(.+)(\.[^.]{1,16})$/.exec(name);
  title.append(text('span', parts?.[1] || name));
  if (parts) title.append(text('span', parts[2]));
  title.title = name;
  return title;
}
function renderRecent(items) {
  const root = byId('recent');
  items = items.slice(0, 4);
  const signature = JSON.stringify(items);
  if (root.dataset.signature === signature) return;
  const openRecords = new Set([...root.querySelectorAll('.receipt-files:not([hidden])')].map(node => node.id));
  root.dataset.signature = signature;
  root.replaceChildren();
  if (!items.length) {
    root.append(text('div', '还没有接收记录', 'empty'));
    return;
  }
  for (const item of items) {
    const record = text('div', '', 'receipt');
    const row = text('div', '', 'receipt-row');
    const symbol = text('div', '', 'receipt-icon');
    symbol.append(icon('arrow'));
    const info = text('div', '', 'receipt-info');
    const side = text('div', date(item.timestamp), 'receipt-side');
    row.append(symbol, info, side);
    record.append(row);
    root.append(record);
    if (!item.files) {
      info.append(text('strong', item.device || '已授权手机'));
      info.append(text('p', (item.mime === 'image/png' ? 'PNG' : 'JPEG') + ' · ' + bytes(item.bytes)));
      side.append(text('span', '已写入剪贴板'));
      continue;
    }
    const counts = {};
    for (const file of item.files) counts[file.category] = (counts[file.category] || 0) + 1;
    const title = Object.keys(receiveCategory).filter(key => counts[key])
      .map(key => counts[key] + ' ' + receiveCategory[key]).join(' · ');
    const complete = item.files.length === item.total;
    if (complete && item.total === 1) info.append(receiptName(item.files[0].name));
    else info.append(text('strong', title || '文件'));
    const result = {
      image: '已保存 · 可直接粘贴',
      files: '已保存 · 可作为文件粘贴',
      failed: '已保存 · 图片未写入剪贴板',
      unchanged: '已保存至目录',
    };
    const status = ' · ' + (complete ? (result[item.clipboard] || '已保存至目录') : '已保存 ' + item.files.length + '/' + item.total);
    const source = item.device || '已授权手机';
    const description = (complete && item.total === 1 ? title + ' · ' : '') + source + status;
    const metadata = text('p', description);
    metadata.title = description;
    const meta = text('div', '', 'receipt-meta');
    meta.append(metadata);
    info.append(meta);
    if (item.total > 1) {
      const files = text('ul', '', 'receipt-files');
      files.id = 'receipt-files-' + item.id;
      files.hidden = !openRecords.has(files.id);
      const toggle = text('button', files.hidden ? '查看文件' : '收起文件', 'text-button');
      toggle.type = 'button';
      toggle.setAttribute('aria-controls', files.id);
      toggle.setAttribute('aria-expanded', String(!files.hidden));
      toggle.onclick = () => {
        files.hidden = !files.hidden;
        toggle.textContent = files.hidden ? '查看文件' : '收起文件';
        toggle.setAttribute('aria-expanded', String(!files.hidden));
      };
      meta.append(toggle);
      for (const file of item.files) files.append(text('li', file.name));
      record.append(files);
    }
  }
}

async function refreshReceipts(includeLegacy, directoryOnly) {
  const legacy = includeLegacy && window.pywebview?.api
    ? bridge('get_legacy_receipts').catch(() => legacyReceipts) : Promise.resolve(legacyReceipts);
  try {
    const [received, previous] = await Promise.all([request('/api/received'), legacy]);
    legacyReceipts = previous;
    byId('receiveSettings').hidden = !window.pywebview?.api;
    byId('receiveDirectory').textContent = received.root;
    if (directoryOnly) return;
    byId('openReceiveRoot').hidden = false;
    byId('receiptError').hidden = true;
    byId('recent').hidden = false;
    const batches = received.batches.map(batch => ({...batch, timestamp:batch.updated * 1000}));
    const items = [...batches, ...legacyReceipts].sort((a, b) => +new Date(b.timestamp) - +new Date(a.timestamp));
    renderRecent(items);
  } catch {
    byId('receiveSettings').hidden = true;
    if (directoryOnly) return;
    byId('openReceiveRoot').hidden = true;
    legacyReceipts = await legacy;
    const root = byId('recent');
    if (!root.querySelector('.receipt')) renderRecent(legacyReceipts);
    root.hidden = !root.querySelector('.receipt');
    byId('receiptError').hidden = false;
  }
}
function renderSettings(settings, state) {
  settingsState=settings;
  if (settings) {
    const startup=settings.startup;
    byId('startupDescription').textContent=startup.description;
    const sw=byId('startupSwitch');
    sw.disabled=false;
    sw.setAttribute('aria-checked', String(startup.enabled));
    sw.classList.toggle('mixed', startup.registered && !['enabled','system_disabled'].includes(startup.state));
    sw.title=startup.description;
    byId('removeStartup').hidden=!startup.registered;
    const notify=byId('notificationSwitch');
    notify.disabled=!settings.notificationsSupported;
    notify.setAttribute('aria-checked', String(settings.preferences.notifications));
    if(!settings.notificationsSupported)byId('notificationDescription').textContent='当前 Windows 托盘不支持通知，文件接收和剪贴板文字仍可使用。';
    const fw=settings.firewall;
    byId('firewallStatus').textContent=fw.available===false?fw.reason:fw.authorized===true
      ?'已授权 · '+(fw.interface||'当前网卡')+' · '+(fw.profile||'当前网络'):fw.reason||'尚未授权局域网接收';
    byId('authorizeFirewall').hidden=fw.available===false||(fw.authorized===true&&nativeState?.running===true);
  }
  if (!state) return;
  if(!copyqPathDirty && document.activeElement!==byId('copyqPath'))byId('copyqPath').value=state.copyqPath||'';
  const installed=state.copyqInstalled===true,running=settings?.copyqRunning;
  byId('copyqBadge').textContent=running===true?'正在运行':running===false?(installed?'未运行':'未检测到'):'运行状态未确认';
  byId('copyqBadge').className='plain-status'+(running?' success':'');
  byId('copyqStatus').textContent=(installed?'已检测到程序 · '+state.copyqVersion:'未检测到已验证的 CopyQ 程序')
    +(running===false?' · 接收时将使用 Windows 原生剪贴板':'');
}

function renderReceiverState(state) {
  const running=Boolean(state) && (!window.pywebview?.api || nativeState?.running === true);
  const waiting=nativeState?.serviceState==='WAITING';
  const stopped=['STOPPED','FAILED','STOPPING'].includes(nativeState?.serviceState);
  byId('serviceStatus').textContent=running?'接收服务运行中':waiting?'局域网接收尚未启用':stopped?'服务已停止':'服务异常';
  byId('serviceDot').className='dot'+(running?'':waiting?' waiting':' error');
  byId('retryState').hidden=running||waiting;
  if (!state) {
    byId('networkSummary').textContent='接收端状态未确认';
    renderHomeActions(0,false);
    return;
  }
  byId('appVersion').textContent='版本 '+(nativeState?.version || state.appVersion);
  byId('computerName').textContent=state.computerName;
  byId('networkSummary').textContent='局域网：'+(state.interface||'网卡信息未取得');
  byId('deviceTotal').textContent='已授权手机 · '+state.devices.length+' 台';
  renderDevices(state.devices);
  renderHomeActions(state.devices.length,running);
  if (byId('pairDialog').open && qrDeadline && !state.pairingActive && Number(state.lastPairing?.time)>pairStartedAt) {
    clearInterval(qrTimer);
    qrDeadline=0;
    byId('pairWaiting').hidden=true;
    const dialog=byId('pairDialog');
    dialog.classList.add('pair-complete');
    dialog.setAttribute('aria-labelledby','pairSuccessTitle');
    byId('pairedPhoneName').textContent=state.lastPairing.device||'已授权手机';
    byId('pairSuccess').hidden=false;
    byId('completePair').focus();
  }
}

async function queueRefresh(...parts) {
  if (!desktopVisible) return;
  for (const part of parts) pendingRefresh.add(part);
  if (refreshWork) return refreshWork;
  refreshWork=(async()=>{
    while (desktopVisible && pendingRefresh.size) {
      const parts=new Set(pendingRefresh);
      pendingRefresh.clear();
      const health=parts.has('health');
      const state=health || parts.has('devices') || parts.has('settings');
      const native=Boolean(window.pywebview?.api);
      const work=[];
      if (state) {
        work.push(Promise.all([
          request('/api/state').catch(()=>null),
          health && native ? bridge('get_state').catch(()=>null) : nativeState,
        ]).then(([nextState, healthState])=>{
          receiverState=nextState;
          nativeState=healthState;
          renderReceiverState(receiverState);
        }));
      }
      if (parts.has('receipts') || parts.has('legacy') || parts.has('directory')) {
        work.push(refreshReceipts(parts.has('legacy'), parts.has('directory') && !parts.has('receipts') && !parts.has('legacy')));
      }
      if (parts.has('settings')) {
        const settings=native ? bridge('get_settings') : Promise.resolve(null);
        work.push(Promise.all([settings, work[0]]).then(([value])=>{
          if (nativeState && value) {
            nativeState.firewall=value.firewall;
            renderReceiverState(receiverState);
          }
          renderSettings(value, receiverState);
        }).catch(()=>toast('无法读取桌面设置', true)));
      }
      await Promise.all(work);
    }
  })();
  try { await refreshWork; }
  finally { refreshWork=null; }
}

function refresh() {
  if (activePage==='home') return queueRefresh('health','legacy');
  if (activePage==='clipboard') return Promise.all([queueRefresh('settings'), refreshArchive()]);
  if (activePage==='settings') return queueRefresh('settings','directory');
  return queueRefresh('devices');
}

window.morypasteWindowVisible=visible=>{
  if (desktopVisible===visible) return;
  desktopVisible=visible;
  if (visible) {
    refresh();
  } else {
    pendingRefresh.clear();
  }
};
if (!window.chrome?.webview) {
  document.addEventListener('visibilitychange',()=>window.morypasteWindowVisible(!document.hidden));
  setInterval(()=>{
    if (desktopVisible && activePage==='home' && !byId('homeDevicePanel').hidden
        && receiverState?.devices.some(device=>device.clipboardSync)) {
      queueRefresh('devices');
    }
  }, 2000);
} else {
  window.chrome.webview.addEventListener('message',({data})=>{
    if (data?.type==='morypasteWindowVisible') window.morypasteWindowVisible(data.value);
    if (data?.type==='morypasteReceiverChanged') window.morypasteReceiverChanged(data.value);
    if (data?.type==='morypasteUpdateProgress') {
      byId('updateStatus').textContent=data.value===null?'正在下载安装包…':`正在下载安装包 · ${data.value}%`;
    }
  });
}
window.morypasteReceiverChanged=kind=>{
  if (kind==='status') { queueRefresh('health'); return; }
  if (kind==='devices' && (activePage==='home' || byId('pairDialog').open)) {
    queueRefresh('devices');
  } else if (activePage==='home' && (kind==='files' || kind==='legacy')) {
    queueRefresh(kind==='legacy'?'legacy':'receipts');
  }
};


let pairDialogGeneration = 0;
function resetPairDialog() {
  clearInterval(qrTimer);
  qrTimer = null;
  qrDeadline = 0;
  return ++pairDialogGeneration;
}

async function pair() {
  const dialog = byId('pairDialog');
  const generation = resetPairDialog();
  dialog.classList.remove('pair-complete', 'android-guide');
  dialog.setAttribute('aria-labelledby', 'pairTitle');
  byId('pairTitle').hidden = false;
  byId('androidGuide').hidden = true;
  byId('pairWaiting').hidden = false;
  byId('pairSuccess').hidden = true;
  if (!dialog.open) dialog.showModal();
  pairStartedAt = Date.now() / 1000;
  byId('qrimg').replaceChildren();
  byId('pairResult').textContent = '';
  byId('expiry').textContent = '正在准备一次性二维码';
  const regenerate = byId('regeneratePair');
  regenerate.disabled = true;

  try {
    const r = await request('/api/pairing', {});
    if (generation !== pairDialogGeneration) return;
    const svg = new DOMParser().parseFromString(r.svg, 'image/svg+xml').documentElement;
    if (svg.tagName.toLowerCase() !== 'svg' || svg.querySelector('script,foreignObject')) {
      throw Error('二维码格式无效');
    }
    byId('qrimg').replaceChildren(document.importNode(svg, true));
    qrDeadline = Date.now() + r.expiresIn * 1000;
    const tick = () => {
      const left = Math.max(0, Math.ceil((qrDeadline - Date.now()) / 1000));
      byId('expiry').textContent = left
        ? '有效时间 ' + Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0')
        : '二维码已过期，请重新生成。';
      if (!left) clearInterval(qrTimer);
      else if (!window.pywebview?.api && left % 2 === 0) queueRefresh('devices');
    };
    qrTimer = setInterval(tick, 1000);
    tick();
    queueRefresh('devices');
  } catch (error) {
    if (generation === pairDialogGeneration) byId('expiry').textContent = '生成失败：' + error.message;
  } finally {
    if (generation === pairDialogGeneration) regenerate.disabled = false;
  }
}

async function startPairing() {
  if (pairingAuthorization) return;
  if (window.pywebview?.api && (!nativeState?.running || nativeState.firewall?.authorized === false)) {
    const dialog = byId('pairDialog');
    if (dialog.open) closeDialog(dialog);
    pairingAuthorization = true;
    lanPairError = '';
    const buttons = [...document.querySelectorAll('[data-add-device]')];
    buttons.forEach(button => button.disabled = true);
    renderReceiverState(receiverState);
    try {
      const result = await bridge('authorize_firewall');
      if (!result.serviceReady) {
        lanPairError = result.message;
        return;
      }
    } catch {
      lanPairError = '无法启用局域网接收，请重试。';
      return;
    } finally {
      pairingAuthorization = false;
      buttons.forEach(button => button.disabled = false);
      renderReceiverState(receiverState);
      queueRefresh('health');
    }
  }
  await pair();
}

document.querySelectorAll('[data-add-device]').forEach(button => button.onclick = startPairing);
byId('regeneratePair').onclick = pair;

async function showAndroidGuide() {
  const dialog = byId('pairDialog');
  const generation = resetPairDialog();
  dialog.classList.remove('pair-complete');
  dialog.classList.add('android-guide');
  dialog.setAttribute('aria-labelledby', 'androidGuideTitle');
  for (const id of ['pairTitle', 'pairWaiting', 'pairSuccess']) byId(id).hidden = true;
  byId('androidGuide').hidden = false;
  if (!dialog.open) dialog.showModal();
  byId('backToPair').focus();

  const image = byId('androidDownloadQr');
  const status = byId('androidDownloadStatus');
  image.hidden = true;
  status.textContent = '正在准备下载二维码…';
  const open = byId('openAndroidRelease');
  open.disabled = true;
  try {
    const result = await bridge('get_android_download_guide');
    if (generation !== pairDialogGeneration) return;
    image.src = result.qr;
    image.hidden = false;
    status.textContent = '';
  } catch {
    if (generation === pairDialogGeneration) status.textContent = '二维码暂时不可用，可以打开下载页。';
  } finally {
    if (generation === pairDialogGeneration) open.disabled = false;
  }
}

function enableDesktopLinks() {
  const visible = Boolean(window.pywebview?.api);
  document.querySelectorAll('[data-android-guide]').forEach(button => {
    button.hidden = !visible;
    button.onclick = showAndroidGuide;
  });
  byId('openFeedback').hidden = !visible;
}

window.addEventListener('pywebviewready', enableDesktopLinks);
window.addEventListener('pywebviewready', () => { if (activePage === 'clipboard') refreshArchive(); });
window.addEventListener('focus', () => { if (activePage === 'clipboard') refreshArchive(); });

let archiveState, archiveDraft;
let archiveBusy = false, archiveStorageDirty = false, archiveStorageDraft;
const storageUsage = value => value < 1048576 ? (value / 1024).toFixed(1) + ' KB' : (value / 1048576).toFixed(1) + ' MB';
const archiveLabel = ([mods, key]) => [
  mods & 2 ? 'Ctrl' : '', mods & 1 ? 'Alt' : '', mods & 4 ? 'Shift' : '', key,
].filter(Boolean).join(' + ');

function renderArchive(state) {
  byId('clipboardArchive').hidden = !state.available;
  if (!state.available) return;
  archiveState = state;
  byId('archiveEnabled').checked = !!state.enabled;
  byId('archiveEnabled').disabled = archiveBusy || !!state.error || (!state.enabled && !state.windowsEnabled);
  byId('archiveWindows').hidden = !!state.windowsEnabled || !!state.error;
  for (const id of ['archiveView', 'archiveEdit', 'archiveBinding', 'archiveClear']) {
    byId(id).hidden = !!state.error;
  }
  if (state.error) {
    byId('archiveStatus').textContent = state.error;
    return;
  }
  if (state.storage) {
    if (!archiveStorageDirty) {
      byId('archiveDays').value = state.storage.days;
      byId('archiveImageLimit').value = state.storage.imageLimit;
    }
    byId('archiveUsage').textContent = '文字 ' + storageUsage(state.storage.textBytes) + ' · 图片 ' + storageUsage(state.storage.imageBytes);
  }
  byId('archiveBinding').textContent = archiveLabel([state.mods, state.key]);
  byId('archiveStatus').textContent = !state.windowsEnabled ? 'Windows 历史未开启。已保存的记录仍可查看。'
    : !state.enabled ? '开启后，跟随 Windows 剪贴板历史保存到本机。'
    : state.syncError || (!state.hotkeyReady ? '本地保存已开启。快捷键被占用，请修改。' : '正在跟随 Windows 剪贴板历史。');
}

async function refreshArchive() {
  if (!window.pywebview?.api || archiveBusy) return;
  try {
    const state = await bridge('get_clipboard_archive');
    if (!archiveBusy) renderArchive(state);
  } catch {
    renderArchive({available: true, error: '无法读取本地历史状态。'});
  }
}

async function changeArchive(action, value = null) {
  if (archiveBusy) return false;
  archiveBusy = true;
  byId('archiveEnabled').disabled = true;
  try {
    const state = await bridge('change_clipboard_archive', action, value);
    if (state.error) throw Error(state.error);
    if (typeof state.enabled === 'boolean') renderArchive({available: true, ...state});
    return state;
  } catch (error) {
    toast(error.message, true);
    return false;
  } finally {
    archiveBusy = false;
    if (archiveState) renderArchive(archiveState);
  }
}

byId('archiveEnabled').onchange = event => changeArchive('enable', event.target.checked);
byId('archiveWindows').onclick = () => changeArchive('settings');
byId('archiveView').onclick = () => changeArchive('view');
byId('archiveDays').onchange = byId('archiveImageLimit').oninput = () => { archiveStorageDirty = true; };
byId('archiveFolder').onclick = () => changeArchive('storage_folder');
const retentionDialog = byId('archiveRetentionDialog');
byId('archiveRetentionCancel').onclick = () => retentionDialog.close();

async function saveArchiveStorage(binding, confirmedCount) {
  const state = await changeArchive('storage', [...binding, confirmedCount]);
  if (!state) return;
  if (typeof state.pending === 'number') {
    if (state.pending === 0 && confirmedCount === null) {
      await saveArchiveStorage(binding, 0);
      return;
    }
    archiveStorageDraft = {binding, count: state.pending};
    byId('archiveRetentionMessage').textContent = '此设置将清理 ' + state.pending + ' 条本地记录。系统 Win + V 历史会保留。';
    if (!retentionDialog.open) retentionDialog.showModal();
    return;
  }
  archiveStorageDirty = false;
  renderArchive(archiveState);
  if (retentionDialog.open) retentionDialog.close();
  toast('已保存历史存储设置');
}

byId('archiveSaveStorage').onclick = () => {
  const limit = byId('archiveImageLimit');
  if (!limit.value || !limit.reportValidity()) return;
  saveArchiveStorage([Number(byId('archiveDays').value), Number(limit.value)], null);
};
byId('archiveRetentionConfirm').onclick = () => {
  if (archiveStorageDraft) saveArchiveStorage(archiveStorageDraft.binding, archiveStorageDraft.count);
};

byId('archiveClear').onclick = () => byId('archiveClearDialog').showModal();
byId('archiveKeep').onclick = () => byId('archiveClearDialog').close();
byId('archiveConfirmClear').onclick = async () => {
  if (await changeArchive('clear')) {
    byId('archiveClearDialog').close();
    toast('已清空本地剪贴板历史');
  }
};
const setArchiveDraft = binding => {
  archiveDraft = binding;
  byId('archiveCapture').textContent = archiveLabel(binding);
  byId('archiveShortcutMessage').textContent = '';
};
byId('archiveEdit').onclick = () => {
  setArchiveDraft([archiveState.mods, archiveState.key]);
  byId('archiveShortcutDialog').showModal();
  byId('archiveCapture').focus();
};
byId('archiveCancel').onclick = () => byId('archiveShortcutDialog').close();
byId('archiveReset').onclick = () => setArchiveDraft([7, 'V']);
byId('archiveCapture').onkeydown = event => {
  if (['Escape', 'Tab'].includes(event.key)) return;
  event.preventDefault();
  if (['Control', 'Alt', 'Shift', 'Meta'].includes(event.key)) return;
  if (event.metaKey || !(event.ctrlKey || event.altKey) || !/^Key[A-Z]$/.test(event.code)) {
    byId('archiveShortcutMessage').textContent = '请使用 Ctrl 或 Alt 搭配一个英文字母。';
    return;
  }
  const mods = (event.ctrlKey ? 2 : 0) | (event.altKey ? 1 : 0) | (event.shiftKey ? 4 : 0);
  setArchiveDraft([mods, event.code.slice(3)]);
};
byId('archiveSave').onclick = async () => {
  if (await changeArchive('shortcut', archiveDraft)) byId('archiveShortcutDialog').close();
};
enableDesktopLinks();
byId('backToPair').onclick = startPairing;
byId('openAndroidRelease').onclick = async () => {
  const dialogGeneration = pairDialogGeneration;
  try {
    const result = await bridge('open_android_download_page');
    if (!result.opened && dialogGeneration === pairDialogGeneration) byId('androidDownloadStatus').textContent = result.message;
  } catch {
    if (dialogGeneration === pairDialogGeneration) byId('androidDownloadStatus').textContent = '暂时无法打开 Android 下载页，请稍后重试。';
  }
};
byId('openFeedback').onclick = () => bridge('open_feedback_page').catch(() => toast('暂时无法打开 GitHub 反馈页。', true));


byId('completePair').onclick=()=>{closeDialog(byId('pairDialog'));if(!byId('homeDevices').hidden)byId('homeDevices').focus();};
byId('cancelRevoke').onclick=()=>closeDialog(byId('confirmDialog'));byId('confirmRevoke').onclick=async()=>{const device=pendingRevoke;if(!device||byId('confirmRevoke').disabled)return;const button=byId('confirmRevoke');button.disabled=true;try{await revokeAndConfirm(device.id);closeDialog(byId('confirmDialog'));toast('已撤销这台手机的授权');await queueRefresh('devices');}catch(e){toast(e.message,true);}finally{button.disabled=false;}};
byId('startupSwitch').onclick=async()=>{const button=byId('startupSwitch');button.disabled=true;try{if(settingsState.startup.state==='system_disabled'){await bridge('open_startup_settings');return;}await bridge('set_startup',!settingsState.startup.registered);await refresh();}catch(e){toast(e.message,true);}finally{button.disabled=false;}};
byId('removeStartup').onclick=async()=>{try{await bridge('set_startup',false);await refresh();toast('已移除登录启动项');}catch(e){toast(e.message,true);}};byId('systemStartup').onclick=()=>bridge('open_startup_settings').catch(e=>toast(e.message,true));
byId('notificationSwitch').onclick=async()=>{try{await bridge('set_notifications',!settingsState.preferences.notifications);await refresh();}catch(e){toast(e.message,true);}};
let copyqPathDirty = false;
byId('copyqPath').addEventListener('input', () => { copyqPathDirty = true; });
byId('saveSettings').onclick=async()=>{const b=byId('saveSettings');b.disabled=true;b.textContent='检查中…';try{const field=byId('copyqPath');const submitted=field.value;const r=await request('/api/settings',{copyqPath:submitted});if(field.value===submitted){field.value=r.copyqPath;copyqPathDirty=false;}await refresh();toast('CopyQ 路径已检查并保存');}catch(e){toast('设置未保存：'+e.message,true);}finally{b.disabled=false;b.textContent='检查并保存';}};
byId('authorizeFirewall').onclick=async()=>{const b=byId('authorizeFirewall');b.disabled=true;try{const result=await bridge('authorize_firewall');lanPairError='';await queueRefresh('health','settings');toast(result.message,!result.serviceReady);}catch{toast('无法启用局域网接收，请重试。',true);}finally{b.disabled=false;}};
byId('exportLogs').onclick = async () => {
  const button = byId('exportLogs');
  button.disabled = true;
  try {
    const result = await bridge('export_logs');
    if (result.saved) toast('日志已导出');
  } catch (error) {
    toast(error.message || '导出日志失败', true);
  } finally {
    button.disabled = false;
  }
};
byId('checkUpdates').onclick = async () => {
  const button = byId('checkUpdates');
  const status = byId('updateStatus');
  const notes = byId('updateNotes');
  const download = byId('openUpdatePage');
  const install = byId('installUpdate');
  button.disabled = true;
  status.hidden = false;
  status.textContent = '正在检查更新…';
  notes.hidden = true;
  download.hidden = true;
  install.hidden = true;
  try {
    const result = await bridge('check_for_updates');
    if (result.status === 'available') {
      status.textContent = '发现新版本 ' + result.version;
      notes.textContent = result.notes || '';
      notes.hidden = !result.notes;
      download.hidden = false;
      install.hidden = !result.canInstall;
    } else {
      status.textContent = ({
        current: '没有可用更新',
        unconfigured: '更新渠道尚未启用',
        error: result.message || '暂时无法检查更新'
      })[result.status] || '暂时无法检查更新';
    }
  } catch {
    status.textContent = '暂时无法检查更新，请稍后重试。';
  } finally {
    button.disabled = false;
  }
};
byId('installUpdate').onclick = async () => {
  const install = byId('installUpdate');
  const check = byId('checkUpdates');
  const status = byId('updateStatus');
  install.disabled = true;
  check.disabled = true;
  status.textContent = '正在下载，完成后将退出客户端并打开安装器…';
  try {
    const result = await bridge('install_update');
    status.textContent = result.started ? '已校验安装包，正在打开安装器…' : result.message;
  } catch {
    status.textContent = '未能启动更新，请重试或打开发布页。';
  } finally {
    install.disabled = false;
    check.disabled = false;
  }
};
byId('openUpdatePage').onclick = async () => {
  try { await bridge('open_update_page'); }
  catch (error) { toast(error.message || '无法打开下载页面', true); }
};
byId('openReceiveRoot').onclick = async () => {
  try { await openReceiveRoot(); }
  catch (error) { toast(error.message, true); }
};
byId('chooseReceiveDirectory').onclick = async () => {
  const button = byId('chooseReceiveDirectory');
  button.disabled = true;
  try {
    const result = await bridge('choose_receive_directory');
    if (result.changed) {
      byId('receiveDirectory').textContent = result.root;
      toast('接收目录已更改，新文件将保存到这里');
    }
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
};
byId('retryState').onclick=()=>queueRefresh('health');
byId('retryReceipts').onclick=()=>queueRefresh('legacy');
if (desktopVisible) refresh();
