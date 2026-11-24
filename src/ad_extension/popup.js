const fmtY = n => n ? `${String(n).slice(0,4)}-${String(n).slice(4,6)}-${String(n).slice(6)}` : '-';
const LAB = { ok: '정상', login: '로그인 필요', error: '오류', running: '받는 중' };
const PAGE = { ad: 'https://advertising.coupang.com/marketing-reporting/billboard/reports/pa', settle: 'https://wing.coupang.com/tenants/rfm/settlements/home' };
async function draw() {
  const all = await chrome.storage.local.get(['st', 'ss']);
  const S = { ad: all.st || {}, settle: all.ss || {} };
  let running = false;
  document.querySelectorAll('.sec').forEach(sec => {
    const job = sec.dataset.job, st = S[job];
    const box = sec.querySelector('.st');
    box.className = 'st ' + (st.status || '');
    box.textContent = (LAB[st.status] || '아직 실행 전') + (st.msg ? ' · ' + st.msg : '');
    sec.querySelector('.last').textContent = st.lastOkAt ? new Date(st.lastOkAt).toLocaleString('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '-';
    sec.querySelector('.cov').textContent = job === 'ad' ? (st.coveredUntil ? '~ ' + fmtY(st.coveredUntil) : '-')
      : (st.done ? Object.keys(st.done).length + '주' + (st.latestWeekEnd ? ' (~ ' + st.latestWeekEnd + ')' : '') : '-');
    if (st.status === 'running') running = true;
  });
  const p = S.settle.lastPath || S.ad.lastPath;
  document.getElementById('path').textContent = p ? '저장 위치: ' + p.replace(/[^\\/]+$/, '') : '';
  document.getElementById('all').disabled = running;
}
document.getElementById('all').onclick = async () => {
  document.getElementById('all').disabled = true;
  await chrome.runtime.sendMessage({ cmd: 'runAll' });
  draw();
};
document.querySelectorAll('.open').forEach(b => b.onclick = () => chrome.tabs.create({ url: PAGE[b.closest('.sec').dataset.job] }));
chrome.storage.onChanged.addListener(draw);
draw();
