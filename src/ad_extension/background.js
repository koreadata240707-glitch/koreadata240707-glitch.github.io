// 한국데이터 광고·정산 자동 받기
// - 광고: 쿠팡 광고센터 매출 성장 광고 보고서(일별·상품) 최근 14일 → 다운로드 폴더
// - 정산: Wing 로켓그로스 정산현황 판매수수료 리포트 + 입출고비·배송비 리포트 → 다운로드 폴더
// 하루 한 번(아침 6시 이후 크롬이 켜져 있을 때) 자동 + 매출 대시보드의 [정산·광고 받기] 버튼으로 바로 실행.
// 받은 파일은 사무실 PC 수집 프로그램(collector.py)이 가져가서 사이트에 반영.
// 로그인은 사람이 직접 함 (이 확장프로그램은 비밀번호를 다루지 않음).

const VENDOR = 'A01638953';
const START_HOUR = 6;

const JOBS = {
  ad: {
    label: '광고',
    origin: 'https://advertising.coupang.com/',
    page: 'https://advertising.coupang.com/marketing-reporting/billboard/reports/pa',
    isLogin: u => !u.startsWith('https://advertising.coupang.com/') || u.includes('/user/login'),
    loginMsg: '광고센터 로그인이 풀렸어요. 로그인하면 바로 이어서 받습니다.'
  },
  settle: {
    label: '정산',
    origin: 'https://wing.coupang.com/',
    page: 'https://wing.coupang.com/tenants/rfm/settlements/home',
    isLogin: u => !u.startsWith('https://wing.coupang.com/') || /login/i.test(u),
    loginMsg: 'Wing 로그인이 풀렸어요. 로그인하면 바로 이어서 받습니다.'
  }
};

// ---------- 날짜 ----------
const pad = n => String(n).padStart(2, '0');
const ymd = d => d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();
const dayStr = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const fromYmd = n => new Date(Math.floor(n / 10000), Math.floor(n / 100) % 100 - 1, n % 100);
const addDays = (d, k) => { const x = new Date(d); x.setDate(x.getDate() + k); return x; };
const strAdd = (s, k) => dayStr(addDays(new Date(s + 'T00:00:00'), k));

// ---------- 상태 (광고: st, 정산: ss) ----------
const SKEY = { ad: 'st', settle: 'ss' };
async function getState(job) { return (await chrome.storage.local.get(SKEY[job]))[SKEY[job]] || {}; }
async function setState(job, patch) {
  const st = Object.assign(await getState(job), patch);
  await chrome.storage.local.set({ [SKEY[job]]: st });
  await drawBadge();
  return st;
}
async function drawBadge() {
  const all = await chrome.storage.local.get(['st', 'ss']);
  const ss = [all.st || {}, all.ss || {}].map(s => s.status);
  const badge = ss.includes('login') ? '!' : ss.includes('error') ? '×' : ss.includes('running') ? '…' : '';
  chrome.action.setBadgeText({ text: badge });
  chrome.action.setBadgeBackgroundColor({ color: badge === '…' ? '#2563eb' : '#dc2626' });
}
function notify(id, title, message) {
  chrome.notifications.create(id, { type: 'basic', iconUrl: 'icon128.png', title, message, priority: 1 });
}
chrome.notifications.onClicked.addListener(id => {
  chrome.tabs.create({ url: id.startsWith('settle') ? JOBS.settle.page : JOBS.ad.page });
});

// ---------- 일정 ----------
function schedule() {
  chrome.alarms.create('tick', { delayInMinutes: 1, periodInMinutes: 30 });
  chrome.alarms.create('wing', { delayInMinutes: 1, periodInMinutes: 10 });
}
chrome.runtime.onInstalled.addListener(async d => {
  schedule();
  if (d.reason === 'update') { const ss = await getState('settle'); if (!ss.fixedV111) await setState('settle', { done: {}, fixedV111: true }); }
});
chrome.runtime.onStartup.addListener(schedule);
chrome.alarms.onAlarm.addListener(a => {
  if (a.name === 'tick') { maybeRun('ad'); maybeRun('settle'); }
  if (a.name === 'wing') wingSales().catch(() => {});
});

// ---------- Wing 판매분석 매출 (10분마다) ----------
// Wing 홈 '판매분석' 그래프와 같은 숫자(최근 15일, 판매-취소)를 받아 다운로드 폴더에 wing매출.json 으로 저장.
// 수집 프로그램이 이 파일로 대시보드 '순매출'을 Wing과 똑같이 맞춤. 로그인 안 돼 있으면 조용히 건너뜀.
const WING_SALES = 'https://wing.coupang.com/tenants/rfm-ss/api/nudging-card/sale-statistics/data';
async function wingFetch() {
  try {
    const r = await fetch(WING_SALES, { credentials: 'include' });
    const j = await r.json();
    if (j && Array.isArray(j.cardData)) return j;
  } catch (e) {}
  // 쿠키가 안 붙는 경우: 열려 있는 Wing 탭 안에서 받기
  const tabs = await chrome.tabs.query({ url: 'https://wing.coupang.com/*' });
  for (const t of tabs) {
    try {
      const [res] = await chrome.scripting.executeScript({ target: { tabId: t.id }, world: 'MAIN',
        func: async u => { try { return await (await fetch(u, { credentials: 'include' })).json(); } catch (e) { return null; } },
        args: [WING_SALES] });
      if (res && res.result && Array.isArray(res.result.cardData)) return res.result;
    } catch (e) {}
  }
  return null;
}
async function wingSales() {
  const j = await wingFetch();
  if (!j || !j.cardData.length) return;
  const body = JSON.stringify({ fetchedAt: new Date().toISOString(), cardData: j.cardData });
  const sig = JSON.stringify(j.cardData);
  const prev = (await chrome.storage.local.get('wingSig')).wingSig;
  const age = Date.now() - ((await chrome.storage.local.get('wingAt')).wingAt || 0);
  if (prev === sig && age < 60 * 60e3) return;     // 안 바뀌었으면 1시간에 한 번만 저장
  const url = 'data:application/json;base64,' + btoa(unescape(encodeURIComponent(body)));
  try { if (chrome.downloads.setUiOptions) await chrome.downloads.setUiOptions({ enabled: false }); } catch (e) {}
  try { await download(url, `${VENDOR}-wing매출.json`, 'overwrite'); }
  finally { try { if (chrome.downloads.setUiOptions) await chrome.downloads.setUiOptions({ enabled: true }); } catch (e) {} }
  await chrome.storage.local.set({ wingSig: sig, wingAt: Date.now() });
}

// 로그인이 풀려 멈췄다면, 사람이 다시 로그인하는 순간 이어서 받음
chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  if (info.status !== 'complete' || !tab.url) return;
  for (const job of Object.keys(JOBS)) {
    const J = JOBS[job];
    if (!tab.url.startsWith(J.origin) || J.isLogin(tab.url)) continue;
    const st = await getState(job);
    if (st.status === 'login') runJob(job, { tabId });
  }
});

chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (!msg) return;
  if (msg.cmd === 'runNow') { runJob(msg.job || 'ad', { manual: true }).then(reply); return true; }
  if (msg.cmd === 'runAll') { runAll().then(reply); return true; }
});

async function runAll() {
  const a = await runJob('ad', { manual: true });
  const s = await runJob('settle', { manual: true });
  const line = (lab, r) => `${lab}: ` + (r.ok ? (r.summary || '완료') : r.login ? '로그인 필요' : (r.msg || '실패'));
  return { ok: !!(a.ok && s.ok), ad: a, settle: s, msg: line('광고', a) + '\n' + line('정산', s) };
}

async function maybeRun(job) {
  const st = await getState(job);
  const now = new Date();
  if (st.lastOkDay === dayStr(now)) return;
  if (now.getHours() < START_HOUR) return;
  if (st.status === 'running' && Date.now() - (st.startedAt || 0) < 15 * 60e3) return;
  if (st.status === 'login' && st.loginNotified === dayStr(now)) return;
  runJob(job, {});
}

// ---------- 사이트 탭 ----------
function waitComplete(tabId, ms = 45000) {
  return new Promise(res => {
    const done = () => { clearTimeout(t); chrome.tabs.onUpdated.removeListener(f); res(); };
    const t = setTimeout(done, ms);
    function f(id, info) { if (id === tabId && info.status === 'complete') done(); }
    chrome.tabs.onUpdated.addListener(f);
    chrome.tabs.get(tabId).then(tb => { if (tb.status === 'complete') done(); }).catch(done);
  });
}
async function siteTab(job, prefer) {
  const J = JOBS[job];
  if (prefer) { try { const t = await chrome.tabs.get(prefer); if (t.url && t.url.startsWith(J.origin)) return { tab: t, created: false }; } catch (e) {} }
  const tabs = await chrome.tabs.query({ url: J.origin + '*' });
  const ok = tabs.find(t => t.url && !J.isLogin(t.url) && t.status === 'complete');
  if (ok) return { tab: ok, created: false };
  const t = await chrome.tabs.create({ url: J.page, active: false });
  await waitComplete(t.id);
  await new Promise(r => setTimeout(r, 3000));   // 로그인 확인 후 이동이 끝날 때까지
  return { tab: await chrome.tabs.get(t.id), created: true };
}
async function inPage(tabId, func, args) {
  const [inj] = await chrome.scripting.executeScript({ target: { tabId }, world: 'MAIN', func, args });
  const r = inj && inj.result;
  if (!r) throw new Error('페이지에서 실행하지 못함');
  return r;
}

// ================= 광고 (광고센터 페이지 안에서 실행: 사람이 버튼 누르는 것과 같은 요청) =================
const WINDOW_DAYS = 14, BACKFILL_MAX = 90;
function adPlan(st) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const end = addDays(today, -1);
  let start = addDays(end, -(WINDOW_DAYS - 1));
  if (st.coveredUntil) {
    const next = addDays(fromYmd(st.coveredUntil), 1);
    const oldest = addDays(end, -(BACKFILL_MAX - 1));
    if (next < start) start = next < oldest ? oldest : next;
  }
  const ranges = [];
  let e = end;
  while (e >= start) {
    let s = addDays(e, -30); if (s < start) s = start;
    ranges.push([ymd(s), ymd(e)]);
    e = addDays(s, -1);
  }
  return ranges;
}

async function adPageJob(s, e, today) {
  const G = '/marketing-reporting/v2/graphql';
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  async function gql(query, variables) {
    const r = await fetch(G, { method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ query, variables }) });
    if (r.status === 401 || r.status === 403) throw new Error('LOGIN');
    let j; try { j = await r.json(); } catch (x) { throw new Error('LOGIN'); }
    if (j.errors && j.errors.length) {
      const m = j.errors.map(x => x.message).join('; ');
      if (/unauthenticated|unauthorized|login/i.test(m)) throw new Error('LOGIN');
      throw new Error(m);
    }
    return j.data;
  }
  const Qcamp = 'query ($startDate: Int!, $endDate: Int!, $reportType: ReportType!, $rbacReportType: String) { getCampaignList(startDate: $startDate, endDate: $endDate, reportType: $reportType, rbacReportType: $rbacReportType) { id name } }';
  const Qlist = 'query ($reportType: ReportType!, $page: Int!, $pageSize: Int!, $duration: Int!, $onlyScheduledReport: Boolean) { reportList(data: {reportType: $reportType, page: $page, pageSize: $pageSize, duration: $duration, onlyScheduledReport: $onlyScheduledReport}) { reports { id requestDate startDate endDate dateGroup granularity campaignCount status } } }';
  const Qreq = 'mutation ($startDate: Int!, $endDate: Int!, $campaignIds: [ID], $reportType: ReportType!, $dateGroup: DateGroup!, $granularity: Granularity, $excludeIfNoClickCount: Boolean) { requestReport(data: {startDate: $startDate, endDate: $endDate, campaignIds: $campaignIds, reportType: $reportType, dateGroup: $dateGroup, granularity: $granularity, excludeIfNoClickCount: $excludeIfNoClickCount}) { id status } }';
  const list = async () => (await gql(Qlist, { reportType: 'pa', page: 1, pageSize: 30, duration: 90, onlyScheduledReport: false })).reportList.reports || [];
  try {
    if (location.pathname.includes('/user/login')) return { ok: false, login: true };
    const camps = (await gql(Qcamp, { startDate: s, endDate: e, reportType: 'pa', rbacReportType: 'AD_REPORT' })).getCampaignList || [];
    if (!camps.length) return { ok: true, empty: true };
    const ids = camps.map(c => String(c.id));
    const rep = (await list()).find(r => r.startDate === s && r.endDate === e && r.dateGroup === 'daily' && r.granularity === 'vendorItem'
      && r.requestDate === today && r.campaignCount === ids.length && !/fail|error/i.test(r.status));
    let id = rep && rep.id;
    if (!id) {
      const m = await gql(Qreq, { reportType: 'pa', startDate: s, endDate: e, dateGroup: 'daily', granularity: 'vendorItem', excludeIfNoClickCount: false, campaignIds: ids });
      id = m.requestReport.id;
    }
    for (let i = 0; ; i++) {
      const r = (await list()).find(x => String(x.id) === String(id));
      if (r && r.status === 'completed') break;
      if (r && /fail|error/i.test(r.status)) return { ok: false, msg: '광고센터에서 보고서 생성 실패' };
      if (i >= 36) return { ok: false, msg: '보고서 생성이 3분 넘게 걸려서 다음에 다시 시도' };
      await sleep(5000);
    }
    const x = await fetch('/marketing-reporting/v2/api/excel-report?id=' + encodeURIComponent(id), { credentials: 'include' });
    if (!x.ok) return { ok: false, msg: '다운로드 실패 (' + x.status + ')' };
    const ct = x.headers.get('content-type') || '';
    if (!ct.includes('spreadsheet')) return { ok: false, msg: '엑셀 파일이 아님 (' + ct.slice(0, 40) + ')' };
    const u8 = new Uint8Array(await x.arrayBuffer());
    let bin = ''; for (let i = 0; i < u8.length; i += 0x8000) bin += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
    return { ok: true, b64: btoa(bin), size: u8.length };
  } catch (err) {
    const m = String(err && err.message || err);
    return { ok: false, login: m === 'LOGIN', msg: m };
  }
}

async function adWork(tab, st) {
  const today = ymd(new Date());
  const files = [];
  let covered = st.coveredUntil || 0;
  for (const [s, e] of adPlan(st)) {
    await setState('ad', { msg: `${s} ~ ${e} 보고서 받는 중…` });
    const r = await inPage(tab.id, adPageJob, [s, e, today]);
    if (r.login) return { login: true };
    if (!r.ok) throw new Error(r.msg || '알 수 없는 오류');
    if (r.empty) { files.push(`${s}~${e} 광고 없음`); covered = Math.max(covered, e); continue; }
    const path = await download('data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,' + r.b64,
      `${VENDOR}_pa_daily_vendorItem_${s}_${e}.xlsx`, 'overwrite');
    files.push(`${s}~${e} ${Math.round(r.size / 1024)}KB`);
    covered = Math.max(covered, e);
    if (path) await setState('ad', { lastPath: path });
  }
  await setState('ad', { coveredUntil: covered, lastFiles: files });
  return { summary: files.join(', ') || '받을 기간 없음' };
}

// ================= 정산 (Wing 페이지 안에서 실행: 정산현황 화면이 쓰는 것과 같은 요청) =================
async function settlePageJob(mode, keys, rtype) {
  rtype = rtype || 'CATEGORY_TR';
  const tok = decodeURIComponent((document.cookie.match(/(?:^|; )XSRF-TOKEN=([^;]*)/) || [])[1] || '');
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const x = (p, b) => new Promise((res, rej) => {
    const r = new XMLHttpRequest();
    r.open('POST', p); r.withCredentials = true;
    r.setRequestHeader('Content-Type', 'application/json');
    r.setRequestHeader('Accept', 'application/json, text/plain, */*');
    if (tok) r.setRequestHeader('X-XSRF-TOKEN', tok);
    r.onload = () => {
      if (r.status === 401 || r.status === 403) return rej(new Error('LOGIN'));
      try { res(JSON.parse(r.responseText)); } catch (e) { rej(new Error(/<html/i.test(r.responseText) ? 'LOGIN' : '응답 해석 실패 (' + r.status + ')')); }
    };
    r.onerror = () => rej(new Error('Wing 연결 실패'));
    r.send(JSON.stringify(b));
  });
  try {
    if (location.hostname !== 'wing.coupang.com' || /login/i.test(location.pathname)) return { ok: false, login: true };
    if (mode === 'list') {
      const now = Date.now(), iso = t => new Date(t).toISOString();
      const s = await x('/tenants/rfm/v2/settlements/status/api', { startDate: iso(now - 330 * 864e5), endDate: iso(now + 60 * 864e5), searchDateType: 'PAYMENT' });
      const keys = [...new Set((s.settlementStatusReports || []).map(r => r.settlementGroupKey).filter(Boolean))];
      return { ok: true, keys };
    }
    // mode === 'get': 판매수수료 리포트 요청 → 완료될 때까지 기다림 → 다운로드 주소
    let rt = String(Date.now());
    let q = await x('/tenants/rfm/v2/settlements/request-download/api', { sellerReportType: rtype, requestTime: rt, settlementGroupKeys: keys, locale: 'ko' });
    if (q && q.remainingTime > 0) {   // 연속 요청 제한이면 잠깐 기다렸다가 한 번 더
      await sleep(Math.min(90000, q.remainingTime > 1000 ? q.remainingTime : q.remainingTime * 1000) + 1500);
      rt = String(Date.now());
      q = await x('/tenants/rfm/v2/settlements/request-download/api', { sellerReportType: rtype, requestTime: rt, settlementGroupKeys: keys, locale: 'ko' });
      if (q && q.remainingTime > 0) return { ok: false, msg: 'Wing 요청 제한 (잠시 후 다시)' };
    }
    for (let i = 0; ; i++) {
      await sleep(3000);
      const l = await x('/tenants/rfm/v2/settlements/download-list/api', { requestTimeFrom: String(Date.now() - 900e3), requestTimeTo: String(Date.now() + 60e3) });
      const m = (l || []).find(z => String(z.requestTime) === rt);
      const s = m && m.downloadStatus;
      if (s === 'COMPLETED') break;
      if (s && /FAIL|ERROR/i.test(s)) return { ok: false, msg: 'Wing에서 리포트 생성 실패' };
      if (i >= 60) return { ok: false, msg: '리포트 생성이 3분 넘게 걸려서 다음에 다시 시도' };
    }
    const d = await x('/tenants/rfm/v2/settlements/download/api/v2', { requestTime: rt, locale: 'ko' });
    if (!d || !d.url) return { ok: false, msg: '다운로드 주소를 받지 못함' };
    return { ok: true, url: d.url };
  } catch (err) {
    const m = String(err && err.message || err);
    return { ok: false, login: m === 'LOGIN', msg: m };
  }
}

// 받을 정산 주 고르기: 처음 보는 주 + 아직 진행 중이거나 막 끝난 주(매일 다시 받음) + 끝난 뒤 한 번 더
function settleNeed(keys, done, today) {
  return keys.filter(k => {
    const m = k.match(/(\d{4}-\d\d-\d\d)-(\d{4}-\d\d-\d\d)$/);
    if (!m) return false;
    const end = m[2], last = done[k];
    if (!last) return true;
    if (end >= strAdd(today, -3)) return last < today;
    return last < strAdd(end, 4);
  }).sort();
}

async function settleWork(tab, st) {
  const today = dayStr(new Date());
  await setState('settle', { msg: '정산 목록 확인 중…' });
  const L = await inPage(tab.id, settlePageJob, ['list', []]);
  if (L.login) return { login: true };
  if (!L.ok) throw new Error(L.msg || '정산 목록을 못 받음');
  const done = st.fixedV111 ? Object.assign({}, st.done || {}) : {};   // 1.1.1: 여러 주 요청 문제로 빠진 주를 다시 받음
  if (!st.fixedV111) await setState('settle', { done: {}, fixedV111: true });
  const need = settleNeed(L.keys, done, today);
  const got = [];
  for (let i = 0; i < need.length; i += 1) {      // 한 번에 한 주씩 (여러 주를 한 번에 요청하면 첫 주만 들어옴)
    const part = need.slice(i, i + 1);
    const from = part[0].slice(10, 20), to = part[part.length - 1].slice(-10);
    await setState('settle', { msg: `${from} ~ ${to} 정산 리포트 받는 중…` });
    const r = await inPage(tab.id, settlePageJob, ['get', part, 'CATEGORY_TR']);
    if (r.login) return { login: true };
    if (!r.ok) throw new Error(r.msg || '정산 리포트 실패');
    const stamp = new Date().toISOString().replace(/\D/g, '').slice(0, 14);
    const path = await download(r.url, `${VENDOR}-정산_${from}_${to}_${stamp}.xlsx`);
    // 같은 주의 입출고비·배송비 리포트 (프로모션·세이버 할인 확인용)
    await setState('settle', { msg: `${from} ~ ${to} 물류비 리포트 받는 중…` });
    const w = await inPage(tab.id, settlePageJob, ['get', part, 'WAREHOUSING_SHIPPING']);
    if (w.login) return { login: true };
    if (w.ok) await download(w.url, `${VENDOR}-물류비_${from}_${to}_${stamp}.xlsx`);
    part.forEach(k => { done[k] = today; });
    got.push(`${from}~${to}`);
    await setState('settle', { done, lastPath: path || st.lastPath });
  }
  const weeks = L.keys.map(k => k.slice(-10)).sort();
  await setState('settle', { done, latestWeekEnd: weeks[weeks.length - 1] || '' });
  return { summary: got.length ? `${need.length}주 받음 (${got[0].slice(0, 10)} ~ ${got[got.length - 1].slice(-10)})` : '새로 받을 주 없음' };
}

// ---------- 다운로드 ----------
function download(url, filename, conflict) {
  return new Promise((res, rej) => {
    chrome.downloads.download({ url, filename, conflictAction: conflict || 'uniquify', saveAs: false }, id => {
      if (chrome.runtime.lastError || !id) return rej(new Error(chrome.runtime.lastError ? chrome.runtime.lastError.message : '다운로드 실패'));
      const t = setTimeout(() => { chrome.downloads.onChanged.removeListener(f); res(null); }, 60000);
      function f(d) {
        if (d.id !== id || !d.state) return;
        if (d.state.current === 'complete') {
          clearTimeout(t); chrome.downloads.onChanged.removeListener(f);
          chrome.downloads.search({ id }).then(r => res(r[0] && r[0].filename));
        } else if (d.state.current === 'interrupted') {
          clearTimeout(t); chrome.downloads.onChanged.removeListener(f); rej(new Error('다운로드 중단'));
        }
      }
      chrome.downloads.onChanged.addListener(f);
    });
  });
}

// ---------- 실행 ----------
const busy = {};
async function runJob(job, opt) {
  if (busy[job]) return { ok: false, msg: '이미 받는 중' };
  busy[job] = true;
  let created = null;
  try {
    const st = await setState(job, { status: 'running', startedAt: Date.now(), msg: '확인 중…' });
    const { tab, created: c } = await siteTab(job, opt.tabId);
    if (c) created = tab.id;
    if (!tab.url || JOBS[job].isLogin(tab.url)) return await needLogin(job, opt.manual);
    const r = job === 'ad' ? await adWork(tab, st) : await settleWork(tab, st);
    if (r.login) return await needLogin(job, opt.manual);
    await setState(job, { status: 'ok', lastOkDay: dayStr(new Date()), lastOkAt: Date.now(), msg: '완료 · ' + r.summary });
    return { ok: true, summary: r.summary };
  } catch (err) {
    const m = String(err && err.message || err);
    await setState(job, { status: 'error', msg: m, errorAt: Date.now() });
    if (!opt.manual) notify(job + '-err', `${JOBS[job].label} 받기 실패`, m + '\n30분 뒤 다시 시도합니다.');
    return { ok: false, msg: m };
  } finally {
    busy[job] = false;
    if (created) { try { await chrome.tabs.remove(created); } catch (e) {} }
  }
}

async function needLogin(job, manual) {
  const st = await getState(job);
  const today = dayStr(new Date());
  await setState(job, { status: 'login', msg: JOBS[job].loginMsg, loginNotified: today });
  if (manual || st.loginNotified !== today) {
    notify(job + '-login', job === 'ad' ? '쿠팡 광고센터 로그인 필요' : '쿠팡 Wing 로그인 필요',
      '알림을 눌러 로그인해 주세요. 로그인하면 자동으로 이어서 받습니다.');
  }
  return { ok: false, login: true, msg: '로그인 필요' };
}
