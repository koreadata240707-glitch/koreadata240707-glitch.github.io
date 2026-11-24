/**
 * 쿠팡 매출 대시보드 (구글 Apps Script 웹앱)
 * ------------------------------------------------------------
 * 1) 사무실 PC 수집 프로그램(collector.py)이 보내는 데이터를 이 시트에 저장 (doPost)
 * 2) 직원용 웹사이트 제공 - 아이디/비밀번호 로그인, 직원 가입 신청 → 관리자 승인
 *
 * ※ TOKEN은 사무실 PC config.ini 의 [google] token 값과 같아야 합니다.
 * ※ 코드를 고친 뒤에는 [배포 > 배포 관리 > 연필(수정) > 버전: 새 버전 > 배포]를 눌러야 반영됩니다.
 */
const TOKEN = 'PUT_TOKEN_HERE'; // 실제 값은 Apps Script 편집기에만 있음 (공개 저장소라 지움)
const INITIAL_ADMIN_PIN = 'PUT_PIN_HERE';   // 최초 관리자(admin)의 임시 비밀번호 - 첫 로그인 때 바꾸게 됨
const SITE_TITLE = '한국데이터 매출 대시보드';
const COST_SHEET = '원가';
const STAFF_SHEET = '_직원';
const STATUS_SHEET = '_상태';
const SESSION_DAYS = 30;
const BOOK_SHEET = '_원가대장';
const BATCH_SHEET = '_차수';

function ss_() { return SpreadsheetApp.getActiveSpreadsheet(); }
function sheet_(name) { const ss = ss_(); return ss.getSheetByName(name) || ss.insertSheet(name); }

/* ═══════════════════════ 웹사이트 ═══════════════════════ */
function doGet() {
  return HtmlService.createHtmlOutput(loadHtml_() || INDEX_HTML)
    .setTitle(SITE_TITLE)
    .addMetaTag('viewport', 'width=device-width, initial-scale=1, viewport-fit=cover')
    .setFaviconUrl('https://www.gstatic.com/images/branding/product/1x/sheets_2020q4_48dp.png');
}

/* ---------- 캐시 (속도 향상: 시트를 매번 읽지 않도록) ---------- */
function cacheGetJson_(key) {
  const c = CacheService.getScriptCache();
  const n = Number(c.get(key + '_n') || 0);
  if (!n) return null;
  const keys = [];
  for (let i = 0; i < n; i++) keys.push(key + '_' + i);
  const parts = c.getAll(keys);
  let s = '';
  for (let i = 0; i < n; i++) { const p = parts[key + '_' + i]; if (p == null) return null; s += p; }
  try { return JSON.parse(s); } catch (e) { return null; }
}

function cachePutJson_(key, obj, sec) {
  try {
    const s = JSON.stringify(obj);
    const m = {};
    let n = 0;
    for (let i = 0; i < s.length; i += 25000) { m[key + '_' + n] = s.substring(i, i + 25000); n++; }
    m[key + '_n'] = String(n);
    CacheService.getScriptCache().putAll(m, sec || 21600);
  } catch (e) {}
}

function cacheClear_(key) { CacheService.getScriptCache().remove(key + '_n'); }

/** 수집 프로그램이 올려 둔 최신 화면(site.html)이 있으면 그것을 사용 */
function loadHtml_() {
  const ch = cacheGetJson_('html'); if (ch) return ch.h;
  const sh = ss_().getSheetByName('_사이트');
  if (!sh || sh.getLastRow() < 1) return '';
  const h = sh.getRange(1, 1, sh.getLastRow(), 1).getValues().map(r => r[0]).join('');
  cachePutJson_('html', { h: h });
  return h;
}

function setHtml_(html) {
  const sh = sheet_('_사이트');
  sh.clearContents();
  const parts = [];
  for (let i = 0; i < html.length; i += 40000) parts.push([html.substring(i, i + 40000)]);
  if (parts.length) {
    if (sh.getMaxRows() < parts.length) sh.insertRowsAfter(sh.getMaxRows(), parts.length - sh.getMaxRows());
    sh.getRange(1, 1, parts.length, 1).setNumberFormat('@').setValues(parts);
  }
  if (!sh.isSheetHidden()) sh.hideSheet();
  cacheClear_('html');
}

/* ---------- 로그인 / 세션 (아이디 + 비밀번호) ---------- */
const ACCOUNT_SHEET = '_계정';
// 열: 아이디, 이름, 비밀번호해시, 솔트, 권한, 상태(대기/사용/중지), 마지막 접속, 가입일, 비밀번호변경필요(Y/N)

function hash_(pw, salt) {
  let h = salt + '|' + pw;
  for (let i = 0; i < 300; i++) {
    h = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, h + salt)
      .map(b => ('0' + (b & 255).toString(16)).slice(-2)).join('');
  }
  return h;
}

function accountSheet_() {
  const ss = ss_();
  let sh = ss.getSheetByName(ACCOUNT_SHEET);
  if (!sh) {
    sh = ss.insertSheet(ACCOUNT_SHEET);
    sh.getRange('A:A').setNumberFormat('@');
    sh.getRange(1, 1, 1, 9).setValues([['아이디', '이름', '비밀번호해시', '솔트', '권한', '상태', '마지막 접속', '가입일', '변경필요']])
      .setFontWeight('bold');
    sh.setFrozenRows(1);
    sh.hideSheet();
  }
  if (sh.getLastRow() < 2) {   // 최초 관리자: 아이디 admin / 임시 비밀번호 = 예전 관리자 PIN (첫 로그인 때 변경)
    const salt = Utilities.getUuid();
    sh.getRange(2, 1, 1, 9).setValues([['admin', '대표(관리자)', hash_(INITIAL_ADMIN_PIN, salt), salt, '관리자', '사용', '', now_(), 'Y']]);
  }
  return sh;
}

function readAccounts_() {
  const ca = cacheGetJson_('acct'); if (ca) return ca;
  const sh = accountSheet_();
  const n = sh.getLastRow();
  if (n < 2) return [];
  const list = sh.getRange(2, 1, n - 1, 9).getDisplayValues().map((r, i) => ({
    row: i + 2, id: String(r[0]).trim().toLowerCase(), name: r[1], hash: r[2], salt: r[3],
    role: r[4] === '관리자' ? '관리자' : '직원', status: r[5] || '대기', last: r[6], joined: r[7], mustChange: r[8] === 'Y',
  })).filter(a => a.id);
  cachePutJson_('acct', list, 300);
  return list;
}

function publicUser_(a) { return { id: a.id, name: a.name, role: a.role, mustChange: a.mustChange }; }

function login(id, pw) {
  const cache = CacheService.getScriptCache();
  const fails = Number(cache.get('login_fails') || 0);
  if (fails >= 30) return { error: '로그인 시도가 너무 많아요. 10분 뒤에 다시 시도하세요.' };
  id = String(id || '').trim().toLowerCase();
  const a = readAccounts_().find(x => x.id === id);
  if (!a || hash_(String(pw || ''), a.salt) !== a.hash) {
    cache.put('login_fails', String(fails + 1), 600);
    return { error: '아이디 또는 비밀번호가 맞지 않아요.' };
  }
  if (a.status === '대기') return { error: '가입 승인을 기다리는 중이에요. 관리자에게 승인을 요청하세요.' };
  if (a.status !== '사용') return { error: '사용 중지된 계정이에요. 관리자에게 문의하세요.' };
  const token = Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, '');
  const props = PropertiesService.getScriptProperties();
  cleanupSessions_(props);
  props.setProperty('sess_' + token, JSON.stringify({ id: a.id, exp: Date.now() + SESSION_DAYS * 864e5 }));
  accountSheet_().getRange(a.row, 7).setValue(now_()); cacheClear_('acct');
  return { token: token, user: publicUser_(a) };
}

function signup(id, name, pw) {
  id = String(id || '').trim().toLowerCase();
  name = String(name || '').trim();
  pw = String(pw || '');
  if (!/^[a-z0-9_.]{3,20}$/.test(id)) return { error: '아이디는 영문 소문자·숫자 3~20자로 만들어 주세요.' };
  if (!name) return { error: '이름을 입력해 주세요.' };
  if (pw.length < 6) return { error: '비밀번호는 6자 이상으로 만들어 주세요.' };
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    if (readAccounts_().some(a => a.id === id)) return { error: '이미 사용 중인 아이디예요.' };
    const sh = accountSheet_();
    const salt = Utilities.getUuid();
    sh.getRange(sh.getLastRow() + 1, 1, 1, 9).setValues([[id, name, hash_(pw, salt), salt, '직원', '대기', '', now_(), 'N']]); cacheClear_('acct');
  } finally { lock.releaseLock(); }
  return { ok: true };
}

function resume(token) {
  const u = session_(token);
  return u ? { user: publicUser_(u) } : { authError: true };
}

function logout(token) {
  PropertiesService.getScriptProperties().deleteProperty('sess_' + token);
  return { ok: true };
}

function session_(token) {
  if (!token) return null;
  const raw = PropertiesService.getScriptProperties().getProperty('sess_' + token);
  if (!raw) return null;
  const s = JSON.parse(raw);
  if (s.exp < Date.now() || !s.id) { logout(token); return null; }
  const u = readAccounts_().find(x => x.id === s.id && x.status === '사용');   // 사용 중지되면 즉시 차단
  if (!u) { logout(token); return null; }
  return u;
}

function cleanupSessions_(props) {
  const all = props.getProperties();
  Object.keys(all).forEach(k => {
    if (k.indexOf('sess_') === 0) {
      try { if (JSON.parse(all[k]).exp < Date.now()) props.deleteProperty(k); } catch (e) { props.deleteProperty(k); }
    }
  });
}

function guard_(token, adminOnly) {
  const u = session_(token);
  if (!u) return { fail: { authError: true } };
  if (adminOnly && u.role !== '관리자') return { fail: { error: '관리자만 사용할 수 있어요.' } };
  return { user: u };
}

function changePassword(token, oldPw, newPw) {
  const g = guard_(token); if (g.fail) return g.fail;
  const a = g.user;
  if (hash_(String(oldPw || ''), a.salt) !== a.hash) return { error: '지금 비밀번호가 맞지 않아요.' };
  newPw = String(newPw || '');
  if (newPw.length < 6) return { error: '새 비밀번호는 6자 이상으로 만들어 주세요.' };
  const salt = Utilities.getUuid();
  const sh = accountSheet_();
  sh.getRange(a.row, 3, 1, 2).setValues([[hash_(newPw, salt), salt]]);
  sh.getRange(a.row, 9).setValue('N'); cacheClear_('acct');
  return { ok: true };
}

/* ---------- 데이터 ---------- */
function getData(token) {
  const g = guard_(token); if (g.fail) return g.fail;
  const cached = cacheGetJson_('data'); if (cached) return cached;
  const kpiRows = table_('_요약지표');
  const kpi = {};
  kpiRows.forEach(r => {
    let v = r['값']; const key = String(r['항목'] || '');
    // 숫자 칸이 시트에서 날짜로 잘못 바뀐 경우 되돌림 (날짜 항목은 그대로)
    const m = typeof v === 'string' && /^(\d{4,6})-(\d\d)-(\d\d)$/.exec(v);
    if (m && !/날짜|일$|업데이트|시각/.test(key)) v = Math.round((Date.UTC(+m[1], +m[2] - 1, +m[3]) - Date.UTC(1899, 11, 30)) / 864e5);
    kpi[key] = v;
  });
  const status = ss_().getSheetByName(STATUS_SHEET) ? String(ss_().getSheetByName(STATUS_SHEET).getRange('A1').getValue() || '') : '';
  const out = {
    kpi: kpi,
    updated: kpi['마지막 업데이트'] || '',
    status: status,
    daily: table_('일별요약'),
    monthly: table_('월별요약'),
    products: table_('상품별순위(최근30일)'),
    reasons: table_('반품취소현황(최근90일)'),
    inventory: table_('재고현황'),
    settlements: table_('정산내역'),
    recentReturns: table_('반품취소상세', 100),
    adDaily: table_('광고_일별', 400),
    adProducts: table_('광고_상품별(최근30일)'),
    optDaily: table_('옵션일별'),
    optNames: table_('옵션이름'),
    logiMonthly: table_('물류비월별'),
    logiProd: table_('물류비상품별'),
    groups: readBook_().filter(o => (o.opts || []).length).map(o => ({ name: o.name, batch: o.batch, opts: o.opts.map(x => [x, Number((o.optSet || {})[x]) || 1]) })),
  };
  cachePutJson_('data', out);
  return out;
}

/** 시트를 [{머리글: 값}] 배열로 읽기 (날짜는 문자열로 변환) */
function table_(name, limit) {
  const sh = ss_().getSheetByName(name);
  if (!sh || sh.getLastRow() < 2) return [];
  const n = Math.min(sh.getLastRow() - 1, limit || 100000);
  const ncol = sh.getLastColumn();
  const head = sh.getRange(1, 1, 1, ncol).getValues()[0];
  const tz = Session.getScriptTimeZone() || 'Asia/Seoul';
  return sh.getRange(2, 1, n, ncol).getValues().map(r => {
    const o = {};
    head.forEach((h, i) => {
      let v = r[i];
      if (v instanceof Date && v.getFullYear() < 1901) v = Math.round((v.getTime() - Date.UTC(1899, 11, 30)) / 864e5);   // 숫자가 날짜로 잘못 바뀐 칸
      else if (v instanceof Date) v = Utilities.formatDate(v, tz, h === '월' || h === '매출인식월' ? 'yyyy-MM' : 'yyyy-MM-dd');
      o[h] = v;
    });
    return o;
  });
}

/* ---------- 원가 (관리자) ---------- */
function getCosts(token) {
  const g = guard_(token, true); if (g.fail) return g.fail;
  const sh = costSheet_();
  const n = sh.getLastRow();
  if (n < 2) return [];
  return sh.getRange(2, 1, n - 1, 4).getDisplayValues()
    .map(r => ({ id: r[0], product: r[1], option: r[2], cost: String(r[3]).replace(/,/g, '') }))
    .filter(r => r.id);
}

function saveCosts(token, changes) {
  const g = guard_(token, true); if (g.fail) return g.fail;
  const sh = costSheet_();
  const n = sh.getLastRow();
  const ids = n >= 2 ? sh.getRange(2, 1, n - 1, 1).getDisplayValues().map(r => String(r[0]).trim()) : [];
  (changes || []).forEach(c => {
    const i = ids.indexOf(String(c.id).trim());
    if (i >= 0) sh.getRange(i + 2, 4).setValue(c.cost === '' ? '' : Number(c.cost));
  });
  return { ok: true };
}

/* ---------- 원가·마진 계산기 (관리자) ---------- */
/* 원가·마진 계산 (사이트 화면과 Apps Script 서버가 같은 식을 씀) */
function cnum(v){ if(v===null||v===undefined) return 0; v=Number(String(v).replace(/,/g,'')); return isFinite(v)?v:0; }
function costCalc(it, b, batchItems){
  b = b || {}; batchItems = batchItems || [it];
  const r = {warn:[]};
  const qty = cnum(it.qty);
  const fx = cnum(it.fx) || cnum(b.fx);
  const agent = cnum(it.agent);   // 비워두면 0% (차수 기본값은 새 상품 만들 때만 채워 넣음)
  if(!fx) r.warn.push('환율');
  // 1) 상품가 (위안 → 원)
  r.goodsCny = cnum(it.cny) * (qty || 1) + cnum(it.ship);
  r.goods = qty ? (r.goodsCny * fx * (1 + agent/100)) / qty : cnum(it.cny) * fx * (1 + agent/100);
  // 2) 차수 공동비용 배분
  const total = (b.costs||[]).reduce((s,c)=>s+cnum(c.amount),0);
  r.batchTotal = total;
  if(!b.name) r.shared = 0;
  else if(b.alloc==='cbm'){
    const sum = batchItems.reduce((s,x)=>s+cnum(x.cbm)*cnum(x.qty),0);
    r.shared = (sum && qty) ? total * (cnum(it.cbm)*qty/sum) / qty : 0;
    if(total && !cnum(it.cbm)) r.warn.push('부피(CBM)');
  } else {
    const den = cnum(b.allocQty) || batchItems.reduce((s,x)=>s+cnum(x.qty),0);
    r.shared = den ? total/den : 0;
  }
  if(b.name && !total) r.warn.push('차수 공동비용');
  // 3) 부자재
  r.material = cnum(it.sticker) + cnum(it.pack);
  // 3-1) 관세 (상품값 × 관세율, 부가세는 환급되므로 제외)
  r.duty = cnum(it.cny) * fx * cnum(it.duty) / 100;
  r.unitBase = r.goods + r.shared + r.material + r.duty;
  // 4) 파손 반영 (실제 팔 수 있는 수량으로 나눔)
  const real = qty - cnum(it.dmg);
  r.unit = (qty && real > 0) ? r.unitBase * qty / real : r.unitBase;
  r.dmgCost = r.unit - r.unitBase;
  // 5) 쿠팡 입고비 (밀크런 / 파레트)
  // 차수 입고: 상품별 입고 방식(파레트/밀크런/나눠서) + 실은 수량
  const inMode=x=>{const v=x.inbound;if(v==='batch'||v==='bpallet'||v==='bmilk'||v==='bsplit')return b.inMode==='pallet'?'bpallet':'bmilk';return v};
  const inQ=x=>{const has=v=>v!==''&&v!=null;
    if(b.inMode==='pallet'&&has(x.palBox)&&['batch','bpallet','bmilk','bsplit'].includes(x.inbound))return {p:cnum(x.palBox)*cnum(x.perBox),m:0};   // 파레트 박스 수 × 박스당 입수
    const m=inMode(x),base=Math.max(0,cnum(x.qty)-cnum(x.dmg));
    return {p:m==='bpallet'?(has(x.inPal)?cnum(x.inPal):base):m==='bsplit'?cnum(x.inPal):0, m:m==='bmilk'?(has(x.inMilk)?cnum(x.inMilk):base):m==='bsplit'?cnum(x.inMilk):0}};
  const im=inMode(it);
  if(im==='bpallet'||im==='bmilk'||im==='bsplit'){
    const q=inQ(it), ptot=cnum(b.pallets)*cnum(b.palletFee), bf=cnum(b.boxFee)||4000;
    const its=batchItems.filter(x=>['bpallet','bmilk','bsplit','batch'].includes(x.inbound));
    const palSum=b.alloc==='cbm'?its.reduce((s,x)=>s+inQ(x).p*cnum(x.cbm),0):its.reduce((s,x)=>s+inQ(x).p,0);
    r.palUnit = palSum ? ptot*(b.alloc==='cbm'?cnum(it.cbm):1)/palSum : 0;   // 파레트로 보낸 1개당
    r.milkUnit = cnum(it.perBox) ? bf/cnum(it.perBox) : 0;                    // 밀크런으로 보낸 1개당
    r.palQ=q.p; r.milkQ=q.m; r.palSum=palSum; r.palTotal=ptot;
    r.inbound = (q.p+q.m) ? (q.p*r.palUnit + q.m*r.milkUnit)/(q.p+q.m) : 0;
    r.inLabel = im==='bpallet'?'차수 파레트':im==='bmilk'?'밀크런':'파레트+밀크런';
    if(q.p&&!ptot) r.warn.push('차수 파레트 요금');
    if(q.m&&!cnum(it.perBox)) r.warn.push('박스당 입수');
    if(im==='bsplit'&&!(q.p+q.m)) r.warn.push('파레트·밀크런 수량');
  } else if(it.inbound==='pallet'){
    const per = cnum(it.perPallet) || cnum(it.palletBoxes) * cnum(it.perBox);
    r.perPallet = per;
    r.inbound = per ? cnum(it.palletFee)/per : 0;
    if(!cnum(it.palletFee)||!per) r.warn.push('파레트 요금/수량');
  } else if(it.inbound==='direct'){
    r.inbound = cnum(it.inboundCost);
  } else if(it.inbound==='none'){
    r.inbound = 0;
  } else {
    r.inbound = cnum(it.perBox) ? cnum(it.boxFee)/cnum(it.perBox) : 0;
    if(!cnum(it.perBox)) r.warn.push('박스당 입수');
  }
  // 비교용: 밀크런·파레트 각각 개당 입고비
  r.cmpMilk = cnum(it.perBox) ? (cnum(it.boxFee)||4000)/cnum(it.perBox) : null;
  const pp = cnum(it.perPallet) || cnum(it.palletBoxes) * cnum(it.perBox);
  r.cmpPallet = (pp && cnum(it.palletFee)) ? cnum(it.palletFee)/pp : null;
  if(r.palUnit) r.cmpPallet = r.palUnit;
  if(r.milkUnit) r.cmpMilk = r.milkUnit;
  const set = cnum(it.set) || 1;
  r.set = set;
  r.landed = (r.unit + r.inbound) * set;           // 판매 1건당 원가 (= 원가 시트에 들어가는 값)
  // 6) 쿠팡 판매 비용
  const price = cnum(it.price), f = cnum(it.fee)/100;
  r.price = price;
  r.feeAmt = price * f;
  r.logi = cnum(it.shipFee) + cnum(it.whFee);
  r.etc = cnum(it.etc);
  r.margin1 = price - r.feeAmt - r.landed;
  r.margin = r.margin1 - r.logi - r.etc;
  r.rate = price ? r.margin/price : 0;
  // 부가세(대략): 매출세액 − 수수료·물류비에 포함된 매입세액
  r.vat = price/11 - (r.feeAmt + r.logi)/11;
  r.marginVat = r.margin - r.vat;
  r.rateVat = price ? r.marginVat/price : 0;
  const fixed = r.landed + r.logi + r.etc;
  r.breakEven = (1-f) > 0 ? fixed/(1-f) : 0;
  const t = cnum(it.target)/100;
  r.targetPrice = (1-f-t) > 0 ? fixed/(1-f-t) : 0;
  r.zeroRoas = r.margin > 0 ? price / r.margin * 100 : 0;
  // 7) 광고 시뮬레이션 (하루 기준 → 30일)
  const spend = cnum(it.adSpend), roas = cnum(it.adRoas)/100;
  r.adSales = spend * roas;
  r.adUnits = price ? r.adSales/price : 0;
  r.adProfit = r.adUnits * r.margin - spend;
  r.organic = cnum(it.organic);
  r.month = (r.adProfit + r.organic * r.margin) * 30;
  if(!price) r.warn.push('판매가');
  return r;
}

const BOOK_HEAD = ['ID', '차수', '품목명', '연결 옵션ID', '판매1건당 원가', '최종 마진', '수정일', '수정자', '데이터(JSON)'];
function bookSheet_() {
  const ss = ss_();
  let sh = ss.getSheetByName(BOOK_SHEET);
  if (!sh) {
    sh = ss.insertSheet(BOOK_SHEET);
    sh.getRange(1, 1, 1, BOOK_HEAD.length).setValues([BOOK_HEAD]).setFontWeight('bold').setBackground('#eef2f7');
    sh.setFrozenRows(1);
    sh.getRange('A:D').setNumberFormat('@');
    sh.hideSheet();
  }
  return sh;
}
function batchSheet_() {
  const ss = ss_();
  let sh = ss.getSheetByName(BATCH_SHEET);
  if (!sh) {
    sh = ss.insertSheet(BATCH_SHEET);
    sh.getRange(1, 1, 1, 3).setValues([['차수', '데이터(JSON)', '수정일']]).setFontWeight('bold').setBackground('#eef2f7');
    sh.setFrozenRows(1);
    sh.getRange('A:A').setNumberFormat('@');
    sh.hideSheet();
  }
  return sh;
}
function readBook_() {
  const sh = bookSheet_(); const n = sh.getLastRow();
  if (n < 2) return [];
  return sh.getRange(2, 1, n - 1, BOOK_HEAD.length).getValues().map(r => {
    let o = {}; try { o = JSON.parse(r[8] || '{}'); } catch (e) {}
    o.id = String(r[0]); o.batch = String(r[1] || o.batch || ''); o.name = String(r[2] || o.name || '');
    o.opts = String(r[3] || '').split(',').map(x => x.trim()).filter(String);
    o.updated = r[6] instanceof Date ? Utilities.formatDate(r[6], 'Asia/Seoul', 'yyyy-MM-dd HH:mm') : String(r[6] || '');
    o.by = String(r[7] || '');
    return o;
  }).filter(o => o.id);
}
function readBatches_() {
  const sh = batchSheet_(); const n = sh.getLastRow();
  if (n < 2) return [];
  return sh.getRange(2, 1, n - 1, 2).getValues().map(r => {
    let o = {}; try { o = JSON.parse(r[1] || '{}'); } catch (e) {}
    o.name = String(r[0]); return o;
  }).filter(o => o.name);
}
function writeBook_(items, who) {
  const sh = bookSheet_();
  const batches = {}; readBatches_().forEach(b => batches[b.name] = b);
  const byBatch = {}; items.forEach(it => (byBatch[it.batch] = byBatch[it.batch] || []).push(it));
  const stamp = now_();
  const rows = items.map(it => {
    const r = costCalc(it, batches[it.batch], byBatch[it.batch]);
    it._landed = Math.round(r.landed); it._margin = Math.round(r.margin);
    const data = Object.assign({}, it); ['id', 'opts', 'updated', 'by', '_landed', '_margin'].forEach(k => delete data[k]);
    return [it.id, it.batch, it.name, (it.opts || []).join(','), it._landed, r.price ? it._margin : '',
            it.updated || stamp, it.by || who || '', JSON.stringify(data)];
  });
  const n = sh.getLastRow();
  if (n > 1) sh.getRange(2, 1, n - 1, BOOK_HEAD.length).clearContent();
  if (rows.length) sh.getRange(2, 1, rows.length, BOOK_HEAD.length).setValues(rows);
  // 원가 시트(D열)에 판매 1건당 원가 반영 → 수집 프로그램 이익 계산에 사용
  const cs = costSheet_(); const cn = cs.getLastRow();
  if (cn >= 2) {
    const ids = cs.getRange(2, 1, cn - 1, 1).getDisplayValues().map(r => String(r[0]).trim());
    const col = cs.getRange(2, 4, cn - 1, 1).getValues();
    const set = {}; items.forEach(it => { const unit = it._landed / (Number(it.set) || 1);
      (it.opts || []).forEach(o => { const n = Number((it.optSet || {})[o]) || 0; set[o] = n ? Math.round(unit * n) : it._landed; }); });
    const was = PropertiesService.getScriptProperties().getProperty('book_opts');
    const prev = was ? was.split(',') : [];
    ids.forEach((id, i) => {
      if (set[id] !== undefined) col[i][0] = set[id];
      else if (prev.indexOf(id) >= 0) col[i][0] = '';   // 연결이 풀린 옵션은 비움
    });
    cs.getRange(2, 4, cn - 1, 1).setValues(col);
    PropertiesService.getScriptProperties().setProperty('book_opts', Object.keys(set).join(','));
  }
  cacheClear_('data');
}
function getCostBook(token) {
  const g = guard_(token, true); if (g.fail) return g.fail;
  return { items: readBook_(), batches: readBatches_(), stats: table_('_옵션실적'), options: getCosts(token) };
}
/** 품목 저장 (여러 개 한 번에). 같은 옵션은 한 품목에만 연결됨 */
function saveCostItems(token, list) {
  const g = guard_(token, true); if (g.fail) return g.fail;
  const lock = LockService.getScriptLock(); lock.waitLock(30000);
  try {
    const items = readBook_();
    const stamp = now_();
    (list || []).forEach(x => {
      if (!x.id) x.id = 'i' + Date.now().toString(36) + Math.floor(Math.random() * 1e4);
      x.updated = stamp; x.by = g.user.name;
      const mine = new Set(x.opts || []);
      items.forEach(o => { if (o.id !== x.id) o.opts = (o.opts || []).filter(v => !mine.has(v)); });
      const i = items.findIndex(o => o.id === x.id);
      if (i >= 0) items[i] = x; else items.push(x);
    });
    writeBook_(items, g.user.name);
    return { ok: true, items: readBook_() };
  } finally { lock.releaseLock(); }
}
function deleteCostItem(token, id) {
  const g = guard_(token, true); if (g.fail) return g.fail;
  const lock = LockService.getScriptLock(); lock.waitLock(30000);
  try { writeBook_(readBook_().filter(o => o.id !== id), g.user.name); return { ok: true }; }
  finally { lock.releaseLock(); }
}
/** 차수 저장 → 그 차수 품목 원가 다시 계산 */
function saveBatch(token, b, oldName) {
  const g = guard_(token, true); if (g.fail) return g.fail;
  if (!b || !String(b.name || '').trim()) return { error: '차수 이름을 입력해 주세요' };
  const lock = LockService.getScriptLock(); lock.waitLock(30000);
  try {
    b.name = String(b.name).trim();
    const sh = batchSheet_(); const n = sh.getLastRow();
    const names = n >= 2 ? sh.getRange(2, 1, n - 1, 1).getDisplayValues().map(r => r[0]) : [];
    let i = names.indexOf(oldName || b.name);
    if (i < 0 && oldName) i = names.indexOf(b.name);
    const data = Object.assign({}, b); delete data.name;
    const row = [b.name, JSON.stringify(data), now_()];
    if (i >= 0) sh.getRange(i + 2, 1, 1, 3).setValues([row]); else sh.appendRow(row);
    const items = readBook_();
    if (oldName && oldName !== b.name) items.forEach(o => { if (o.batch === oldName) o.batch = b.name; });
    writeBook_(items, g.user.name);
    return { ok: true, batches: readBatches_(), items: readBook_() };
  } finally { lock.releaseLock(); }
}
/** 처음 한 번: 기존 스프레드시트(수입원가대장) 내용 가져오기 - 수집 프로그램 토큰으로만 호출 */
function importBook_(req) {
  if (bookSheet_().getLastRow() > 1 && !req.force) return { ok: false, error: '이미 원가 계산기에 내용이 있어서 가져오지 않았어요' };
  const bs = batchSheet_();
  if (bs.getLastRow() > 1) bs.getRange(2, 1, bs.getLastRow() - 1, 3).clearContent();
  const stamp = now_();
  const brow = (req.batches || []).map(b => { const d = Object.assign({}, b); delete d.name; return [b.name, JSON.stringify(d), stamp]; });
  if (brow.length) bs.getRange(2, 1, brow.length, 3).setValues(brow);
  const items = (req.items || []).map(x => Object.assign(x, { updated: stamp, by: '스프레드시트 가져오기' }));
  writeBook_(items, '스프레드시트 가져오기');
  return { ok: true, batches: brow.length, items: items.length };
}

/* ---------- 계정 관리 (관리자) ---------- */
function listUsers(token) {
  const g = guard_(token, true); if (g.fail) return g.fail;
  return readAccounts_().map(a => ({ row: a.row, id: a.id, name: a.name, role: a.role, status: a.status,
    last: a.last, joined: a.joined, me: a.id === g.user.id }));
}

function adminCount_(list) { return list.filter(x => x.status === '사용' && x.role === '관리자').length; }

function setUserStatus(token, row, status) {
  const g = guard_(token, true); if (g.fail) return g.fail;
  if (['사용', '중지'].indexOf(status) < 0) return { error: '잘못된 상태' };
  const list = readAccounts_();
  const a = list.find(x => x.row === row);
  if (!a) return { error: '계정을 찾을 수 없어요.' };
  if (status === '중지' && a.role === '관리자' && a.status === '사용' && adminCount_(list) <= 1)
    return { error: '마지막 관리자는 중지할 수 없어요.' };
  accountSheet_().getRange(row, 6).setValue(status); cacheClear_('acct');
  return { ok: true };
}

function setUserRole(token, row, role) {
  const g = guard_(token, true); if (g.fail) return g.fail;
  role = role === '관리자' ? '관리자' : '직원';
  const list = readAccounts_();
  const a = list.find(x => x.row === row);
  if (!a) return { error: '계정을 찾을 수 없어요.' };
  if (role === '직원' && a.role === '관리자' && adminCount_(list) <= 1) return { error: '관리자가 최소 1명은 있어야 해요.' };
  accountSheet_().getRange(row, 5).setValue(role); cacheClear_('acct');
  return { ok: true };
}

function resetPassword(token, row) {
  const g = guard_(token, true); if (g.fail) return g.fail;
  const a = readAccounts_().find(x => x.row === row);
  if (!a) return { error: '계정을 찾을 수 없어요.' };
  const temp = Utilities.getUuid().replace(/-/g, '').slice(0, 8);
  const salt = Utilities.getUuid();
  const sh = accountSheet_();
  sh.getRange(row, 3, 1, 2).setValues([[hash_(temp, salt), salt]]);
  sh.getRange(row, 9).setValue('Y'); cacheClear_('acct');
  return { ok: true, temp: temp };
}

function now_() { return Utilities.formatDate(new Date(), Session.getScriptTimeZone() || 'Asia/Seoul', 'yyyy-MM-dd HH:mm'); }

/* ═══════════════════════ 보증·교환 ═══════════════════════ */
/* _보증 시트: 종류(product|exchange|serial) · ID · 데이터(JSON) · 수정일 · 수정자 */
const WARRANTY_SHEET = '_보증';
const W_HEAD = ['종류', 'ID', '데이터(JSON)', '수정일', '수정자'];
function wSheet_() {
  const ss = ss_();
  let sh = ss.getSheetByName(WARRANTY_SHEET);
  if (!sh) {
    sh = ss.insertSheet(WARRANTY_SHEET);
    sh.getRange(1, 1, 1, W_HEAD.length).setValues([W_HEAD]).setFontWeight('bold').setBackground('#eef2f7');
    sh.setFrozenRows(1);
    sh.getRange('A:B').setNumberFormat('@');
    sh.hideSheet();
  }
  return sh;
}
function wRead_() {
  const sh = wSheet_(); const n = sh.getLastRow();
  if (n < 2) return [];
  return sh.getRange(2, 1, n - 1, W_HEAD.length).getValues().map((r, i) => {
    let o = {}; try { o = JSON.parse(r[2] || '{}'); } catch (e) {}
    o.kind = String(r[0]); o.id = String(r[1]); o._row = i + 2;
    o.updated = r[3] instanceof Date ? Utilities.formatDate(r[3], 'Asia/Seoul', 'yyyy-MM-dd HH:mm') : String(r[3] || '');
    o.by = String(r[4] || '');
    return o;
  }).filter(o => o.kind && o.id);
}
/** 보증 상품에 연결된 옵션들의 전체 판매 (선입선출 배치 계산용) */
function wSales_(optSet) {
  const sh = ss_().getSheetByName('주문상세');
  if (!sh || sh.getLastRow() < 2 || !Object.keys(optSet).length) return [];
  const v = sh.getRange(2, 1, sh.getLastRow() - 1, 8).getDisplayValues();
  const out = [];
  v.forEach(r => { if (optSet[String(r[4]).trim()]) out.push([r[1] || r[0], String(r[2]), String(r[4]).trim(), Number(String(r[7]).replace(/,/g, '')) || 0]); });
  out.sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
  return out;   // [주문시각, 주문번호, 옵션ID, 수량]
}
function getWarranty(token) {
  const g = guard_(token); if (g.fail) return g.fail;
  const all = wRead_();
  const book = readBook_().map(o => ({ id: o.id, batch: o.batch, name: o.name, opts: o.opts, qty: o.qty, dmg: o.dmg }));
  const products = all.filter(o => o.kind === 'product');
  const optSet = {};
  products.forEach(p => book.forEach(it => { if ((p.items || []).indexOf(it.name) >= 0) (it.opts || []).forEach(x => optSet[x] = 1); }));
  return {
    admin: g.user.role === '관리자',
    products: products,
    exchanges: all.filter(o => o.kind === 'exchange'),
    serials: all.filter(o => o.kind === 'serial'),
    batches: readBatches_().map(b => ({ name: b.name, date: b.date || '' })),
    book: book,
    sales: wSales_(optSet),
  };
}
/** 주문번호로 주문 찾기 (주문상세 시트) */
function lookupOrder(token, orderNo) {
  const g = guard_(token); if (g.fail) return g.fail;
  orderNo = String(orderNo || '').replace(/\D/g, '');
  if (orderNo.length < 6) return { rows: [] };
  const sh = ss_().getSheetByName('주문상세');
  if (!sh || sh.getLastRow() < 2) return { rows: [] };
  const hits = sh.getRange(2, 3, sh.getLastRow() - 1, 1).createTextFinder(orderNo).matchEntireCell(true).findAll();
  const rows = hits.slice(0, 20).map(c => {
    const r = sh.getRange(c.getRow(), 1, 1, 10).getDisplayValues()[0];
    return { date: r[0], time: r[1], orderId: r[2], status: r[3], opt: String(r[4]).trim(), name: r[5], option: r[6], qty: Number(String(r[7]).replace(/,/g, '')) || 0, amount: r[9] };
  });
  return { rows: rows };
}
/** 저장: product·serial 은 관리자만, exchange 는 직원도 가능 */
function saveWarranty(token, kind, obj) {
  if (['product', 'serial', 'exchange'].indexOf(kind) < 0) return { error: '알 수 없는 종류' };
  const g = guard_(token, kind !== 'exchange'); if (g.fail) return g.fail;
  const lock = LockService.getScriptLock(); lock.waitLock(30000);
  try {
    obj = obj || {};
    if (!obj.id) obj.id = (kind === 'serial' ? String(obj.batch || '') : kind[0] + Date.now().toString(36) + Math.floor(Math.random() * 1e4));
    if (!obj.id) return { error: '차수를 골라 주세요' };
    const data = Object.assign({}, obj); ['kind', 'id', '_row', 'updated', 'by'].forEach(k => delete data[k]);
    const json = JSON.stringify(data);
    if (json.length > 49000) return { error: '한 번에 저장하기엔 너무 많아요 (시리얼은 차수별로 나눠 주세요)' };
    const row = [kind, obj.id, json, now_(), g.user.name];
    const hit = wRead_().find(o => o.kind === kind && o.id === obj.id);
    const sh = wSheet_();
    if (hit) sh.getRange(hit._row, 1, 1, W_HEAD.length).setValues([row]); else sh.appendRow(row);
    return { ok: true, id: obj.id };
  } finally { lock.releaseLock(); }
}
function deleteWarranty(token, kind, id) {
  const g = guard_(token, true); if (g.fail) return g.fail;
  const lock = LockService.getScriptLock(); lock.waitLock(30000);
  try {
    const hit = wRead_().find(o => o.kind === kind && o.id === String(id));
    if (hit) wSheet_().deleteRow(hit._row);
    return { ok: true };
  } finally { lock.releaseLock(); }
}

/* ═══════════════════════ 수집 프로그램 수신 ═══════════════════════ */
/* 외부 주소(GitHub Pages)에서 여는 사이트용 API: { fn, args } 로 호출 */
const SITE_FNS = {
  getHtml: () => ({ html: loadHtml_() || INDEX_HTML }),
  login: login, signup: signup, resume: resume, logout: logout, changePassword: changePassword,
  getData: getData, getCosts: getCosts, saveCosts: saveCosts,
  getCostBook: getCostBook, saveCostItems: saveCostItems, deleteCostItem: deleteCostItem, saveBatch: saveBatch,
  getWarranty: getWarranty, lookupOrder: lookupOrder, saveWarranty: saveWarranty, deleteWarranty: deleteWarranty,
  listUsers: listUsers, setUserStatus: setUserStatus, setUserRole: setUserRole, resetPassword: resetPassword,
};

function doPost(e) {
  let req;
  try { req = JSON.parse(e.postData.contents); } catch (err) { return json_({ ok: false, error: '잘못된 요청' }); }
  if (req.fn) {
    const f = SITE_FNS[req.fn];
    if (!f) return json_({ error: '알 수 없는 기능: ' + req.fn });
    try { return json_({ result: f.apply(null, req.args || []) }); }
    catch (err) { return json_({ error: String(err && err.message || err) }); }
  }
  const lock = LockService.getScriptLock();
  lock.waitLock(120000);
  try {
    if (req.token !== TOKEN) return json_({ ok: false, error: '토큰이 일치하지 않습니다' });
    switch (req.action) {
      case 'write': return json_(writeTable_(req));
      case 'get_costs': return json_({ ok: true, costs: costSheet_().getLastRow() < 2 ? [] :
        costSheet_().getRange(2, 1, costSheet_().getLastRow() - 1, 4).getDisplayValues() });
      case 'sync_options': return json_(syncOptions_(req.rows || []));
      case 'status': setStatus_(req.message || ''); cacheClear_('data'); return json_({ ok: true });
      case 'import_book': return json_(importBook_(req));
      case 'add_book': return json_(addBook_(req));
      case 'set_html': setHtml_(String(req.html || '')); return json_({ ok: true });
      case 'finish': setStatus_(''); tidy_(); cacheClear_('data'); return json_({ ok: true });
      default: return json_({ ok: false, error: '알 수 없는 요청: ' + req.action });
    }
  } catch (err) {
    return json_({ ok: false, error: String(err && err.stack || err) });
  } finally {
    lock.releaseLock();
  }
}

/** 수입 원가 계산기에서 보낸 차수 + 상품 추가 (수집 프로그램이 '원가추가_*.json' 을 보냄) */
function addBook_(req) {
  const b = req.batch || {}; let name = String(b.name || '').trim();
  if (!name) throw new Error('차수 이름이 없어요');
  const sh = batchSheet_(); const n = sh.getLastRow();
  const names = n >= 2 ? sh.getRange(2, 1, n - 1, 1).getDisplayValues().map(r => r[0]) : [];
  let base = name, k = 2; while (names.indexOf(name) >= 0) name = base + ' (' + (k++) + ')';
  const data = Object.assign({}, b); delete data.name;
  sh.appendRow([name, JSON.stringify(data), now_()]);
  const items = readBook_(); const stamp = Date.now().toString(36);
  (req.items || []).forEach((x, i) => {
    const it = Object.assign({}, x); delete it._unit; delete it._shared;
    it.id = 'c' + stamp + i; it.batch = name; it.opts = []; it.updated = ''; it.by = '수입 원가 계산기';
    items.push(it);
  });
  writeBook_(items, '수입 원가 계산기');
  return { ok: true, batch: name, items: (req.items || []).length };
}

function json_(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}

function writeTable_(req) {
  const sh = sheet_(req.sheet);
  const header = req.header || [];
  const rows = req.rows || [];
  const ncol = header.length;
  if (req.clear) {
    sh.clearContents();
    sh.clearFormats();
    const need = Math.max((req.total || rows.length) + 1, 2);
    const maxR = sh.getMaxRows();
    if (maxR < need) sh.insertRowsAfter(maxR, need - maxR);
    else if (maxR > need + 20) sh.deleteRows(need + 21, maxR - need - 20);
    if (sh.getMaxColumns() < ncol) sh.insertColumnsAfter(sh.getMaxColumns(), ncol - sh.getMaxColumns());
    sh.getRange(1, 1, 1, ncol).setValues([header]).setFontWeight('bold').setBackground('#eef2f7');
    sh.setFrozenRows(1);
    if (req.hidden && !sh.isSheetHidden()) sh.hideSheet();
  }
  if (rows.length) {
    const start = sh.getLastRow() + 1;
    const maxR = sh.getMaxRows();
    if (start + rows.length - 1 > maxR) sh.insertRowsAfter(maxR, start + rows.length - 1 - maxR);
    const fixed = rows.map(r => {
      const a = r.slice(0, ncol);
      while (a.length < ncol) a.push('');
      return a.map(v => (v === null || v === undefined) ? '' : v);
    });
    sh.getRange(start, 1, fixed.length, ncol).setValues(fixed);
    const fm = req.formats || {};
    Object.keys(fm).forEach(k => sh.getRange(start, Number(k) + 1, fixed.length, 1).setNumberFormat(fm[k]));
  }
  return { ok: true };
}

function costSheet_() {
  const ss = ss_();
  let sh = ss.getSheetByName(COST_SHEET);
  if (!sh) {
    sh = ss.insertSheet(COST_SHEET);
    sh.getRange('A:A').setNumberFormat('@');
    sh.getRange(1, 1, 1, 4).setValues([['옵션ID', '상품명', '옵션명', '원가(1개당, 원)']])
      .setFontWeight('bold').setBackground('#fff4d6');
    sh.setFrozenRows(1);
    sh.getRange('D:D').setNumberFormat('#,##0');
  }
  return sh;
}

function syncOptions_(rows) {
  const sh = costSheet_();
  const n = sh.getLastRow();
  const cur = n >= 2 ? sh.getRange(2, 1, n - 1, 3).getDisplayValues() : [];
  const idx = {}; cur.forEach((r, i) => idx[String(r[0]).trim()] = i);
  const have = new Set(Object.keys(idx));
  const add = []; let fixed = false;
  rows.forEach(r => {
    const id = String(r[0] || '').trim();
    if (id && !have.has(id)) { have.add(id); add.push([id, r[1] || '', r[2] || '', '']); }
    else if (id && idx[id] !== undefined) {   // 임시 이름이었던 옵션은 진짜 이름이 생기면 바꿈
      const c = cur[idx[id]];
      if ((!c[1] || c[1].indexOf('(이름 확인 전') === 0) && r[1] && String(r[1]).indexOf('(이름 확인 전') !== 0) { c[1] = r[1]; c[2] = r[2] || ''; fixed = true; }
    }
  });
  if (fixed) sh.getRange(2, 2, cur.length, 2).setValues(cur.map(c => [c[1], c[2]]));
  if (add.length) sh.getRange(sh.getLastRow() + 1, 1, add.length, 4).setValues(add);
  return { ok: true, added: add.length };
}

function setStatus_(msg) {
  const sh = sheet_(STATUS_SHEET);
  sh.getRange('A1').setValue(msg);
  if (!sh.isSheetHidden()) sh.hideSheet();
}

/** 빈 기본 시트 정리 */
function tidy_() {
  const ss = ss_();
  ['시트1', 'Sheet1'].forEach(n => {
    const s = ss.getSheetByName(n);
    if (s && s.getLastRow() === 0 && ss.getSheets().length > 1) ss.deleteSheet(s);
  });
  // 예전 버전에서 쓰던 시트 정리
  ['_차트_일별', '_차트_월별', '_차트_TOP10', '_차트_반품사유', '주문상세(최근90일)', '매출상세', '대시보드'].forEach(n => {
    const s = ss.getSheetByName(n);
    if (s && ss.getSheets().length > 1) ss.deleteSheet(s);
  });
}

/** 처음 한 번 실행: 권한 승인 + 기본 시트 생성 */
function setup() {
  costSheet_();
  accountSheet_();
  setStatus_('');
  Logger.log('준비 완료! 최초 관리자: admin / ' + INITIAL_ADMIN_PIN);
}

const INDEX_HTML = "<!DOCTYPE html><html><body style=\"font-family:sans-serif;padding:40px;text-align:center\"><h2>사이트 준비 중</h2><p>사무실 PC의 수집 프로그램이 처음 실행되면 화면이 나타나요. 잠시 후 새로고침해 주세요.</p></body></html>";
